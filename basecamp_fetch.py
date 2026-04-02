#!/usr/bin/env python3
"""
Fetch all outstanding (incomplete) Basecamp todos assigned to me.

Reads credentials from a .env file in the same directory.
Automatically refreshes an expired access token and writes
the new token back to .env so future runs work without intervention.

Required .env keys:
  BASECAMP_ACCESS_TOKEN   — OAuth2 bearer token (auto-updated on refresh)
  BASECAMP_ACCOUNT_ID     — numeric Basecamp account ID

Optional .env keys (needed only for auto-refresh):
  BASECAMP_REFRESH_TOKEN  — OAuth2 refresh token
  BASECAMP_CLIENT_ID      — your registered app's client ID
  BASECAMP_CLIENT_SECRET  — your registered app's client secret
"""

import os
import sys
import json
import signal
import logging
import argparse
import requests
from datetime import date
from pathlib import Path

log = logging.getLogger("basecamp")

# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

ENV_PATH = Path(__file__).parent / ".env"

def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def save_env(path: Path, updates: dict) -> None:
    """Update specific keys in .env in-place without touching unrelated lines."""
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines()
    updated = set()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in updates:
            result.append(f'{k}="{updates[k]}"')
            updated.add(k)
        else:
            result.append(line)
    for k, v in updates.items():
        if k not in updated:
            result.append(f'{k}="{v}"')
    path.write_text("\n".join(result) + "\n")

# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def refresh_access_token(env: dict) -> str:
    refresh_token = env.get("BASECAMP_REFRESH_TOKEN")
    client_id     = env.get("BASECAMP_CLIENT_ID")
    client_secret = env.get("BASECAMP_CLIENT_SECRET")

    missing = [k for k, v in {
        "BASECAMP_REFRESH_TOKEN": refresh_token,
        "BASECAMP_CLIENT_ID":     client_id,
        "BASECAMP_CLIENT_SECRET": client_secret,
    }.items() if not v or v.startswith("your_")]

    if missing:
        sys.exit(
            f"Access token expired and auto-refresh is not configured.\n"
            f"Add these to your .env: {', '.join(missing)}"
        )

    print("Access token expired — refreshing...", file=sys.stderr)
    resp = requests.post(
        "https://launchpad.37signals.com/authorization/token",
        data={
            "type":          "refresh",
            "client_id":     client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=TIMEOUT,
    )

    if resp.status_code != 200:
        sys.exit(f"Token refresh failed ({resp.status_code}): {resp.text}")

    data = resp.json()
    new_access_token  = data.get("access_token")
    new_refresh_token = data.get("refresh_token")

    if not new_access_token:
        sys.exit(f"Token refresh response missing access_token: {data}")

    token_updates = {"BASECAMP_ACCESS_TOKEN": new_access_token}
    if new_refresh_token:
        token_updates["BASECAMP_REFRESH_TOKEN"] = new_refresh_token

    save_env(ENV_PATH, token_updates)
    print("Token refreshed and saved to .env ✓", file=sys.stderr)
    return new_access_token

# ---------------------------------------------------------------------------
# Basecamp API helpers
# ---------------------------------------------------------------------------

def make_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent":    "BasecampTodoFetcher (lehgarza@gmail.com)",
    }

TIMEOUT = 15  # seconds per request

def paginate(url: str, headers: dict):
    page = 1
    while url:
        log.debug("  GET %s (page %d)", url, page)
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        items = resp.json()
        log.debug("  -> %d items", len(items))
        yield from items
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url
        page += 1

def get_my_person_id(base_url, headers):
    resp = requests.get(f"{base_url}/my/profile.json", headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    print(f"Logged in as: {data['name']}", file=sys.stderr)
    return data["id"]

def get_projects(base_url, headers):
    return list(paginate(f"{base_url}/projects.json", headers))

def get_todolists(base_url, project_id, todoset_id, headers):
    url = f"{base_url}/buckets/{project_id}/todosets/{todoset_id}/todolists.json"
    return list(paginate(url, headers))

def get_todos(base_url, project_id, todolist_id, headers):
    url = f"{base_url}/buckets/{project_id}/todolists/{todolist_id}/todos.json"
    return list(paginate(url, headers))

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_markdown(todos: list) -> str:
    from collections import OrderedDict
    groups = OrderedDict()
    for todo in todos:
        key = (todo["project"], todo["list"])
        groups.setdefault(key, []).append(todo)

    today = date.today().isoformat()
    lines = [
        "---",
        "type: reference",
        f"date: {today}",
        "tags:",
        "  - type/reference",
        "  - source/basecamp",
        "---",
        "",
        "# Basecamp Tasks",
    ]

    current_project = None
    for (project, lst), items in groups.items():
        if project != current_project:
            lines.append(f"\n## {project}")
            current_project = project
        lines.append(f"\n### {lst}")
        for todo in items:
            entry = f"- [ ] {todo['task']}"
            if todo.get("due"):
                entry += f" — due {todo['due']}"
            entry += f" · [view]({todo['url']})"
            lines.append(entry)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging to stderr")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Global script timeout in seconds (default: 300, 0=none)")
    parser.add_argument("--output", "-o", type=str, action="append",
                        help="Write output to file(s) instead of stdout (repeatable)")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.timeout and hasattr(signal, "SIGALRM"):
        def _timeout_handler(signum, frame):
            sys.exit(f"ERROR: Script timed out after {args.timeout}s")
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(args.timeout)
        log.debug("Global timeout set to %ds", args.timeout)

    env          = load_env(ENV_PATH)
    access_token = env.get("BASECAMP_ACCESS_TOKEN")
    account_id   = env.get("BASECAMP_ACCOUNT_ID")

    if not access_token or not account_id:
        sys.exit("Error: BASECAMP_ACCESS_TOKEN and BASECAMP_ACCOUNT_ID must be set in .env")

    base_url = f"https://3.basecampapi.com/{account_id}"
    headers  = make_headers(access_token)

    def _fetch_assignments():
        log.debug("GET /my/assignments.json")
        resp = requests.get(f"{base_url}/my/assignments.json",
                            headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # Try once; on 401 refresh the token and retry
    try:
        data = _fetch_assignments()
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            access_token = refresh_access_token(env)
            headers      = make_headers(access_token)
            data         = _fetch_assignments()
        else:
            raise

    # Combine priorities and non-priorities into one flat list
    all_items = data.get("priorities", []) + data.get("non_priorities", [])
    log.debug("Got %d assignments (%d priorities, %d non-priorities)",
              len(all_items),
              len(data.get("priorities", [])),
              len(data.get("non_priorities", [])))

    my_todos = []
    for item in all_items:
        if item.get("completed"):
            continue
        my_todos.append({
            "project": item.get("bucket", {}).get("name", "Unknown"),
            "list":    item.get("parent", {}).get("title", "Unknown"),
            "task":    item["content"],
            "due":     item.get("due_on"),
            "url":     item["app_url"],
        })

    if args.format == "markdown":
        output = format_markdown(my_todos)
    else:
        output = json.dumps(my_todos)

    if args.output:
        for path in args.output:
            try:
                Path(path).write_text(output)
                log.debug("Wrote %s", path)
            except PermissionError:
                log.warning("Permission denied writing %s (grant Full Disk Access to Python to fix)", path)
    else:
        print(output)

if __name__ == "__main__":
    main()
