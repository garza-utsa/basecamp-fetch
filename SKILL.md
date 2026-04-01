---
name: basecamp-todos
description: Fetch all outstanding (incomplete) to-dos assigned to me from Basecamp using the Basecamp REST API. Use this skill when the user asks about their Basecamp tasks, todos, outstanding work items, or wants to see what's assigned to them in Basecamp.
---
 
# Basecamp Outstanding Tasks Skill
 
Fetch all outstanding (incomplete) to-dos assigned to the authenticated user across all their Basecamp projects.
 
---
 
## Prerequisites
 
You need a Basecamp OAuth2 access token and your account ID. See the **Authentication Setup** section below if you don't have these yet.
 
---
 
## Authentication Setup (one-time)
 
### 1. Register your app
Go to [launchpad.37signals.com/integrations](https://launchpad.37signals.com/integrations) and register a new integration. You'll receive a `client_id` and `client_secret`. Set the redirect URI to `http://localhost` (for personal/script use).
 
### 2. Get an authorization code
Visit this URL in your browser (replace values):
```
https://launchpad.37signals.com/authorization/new?response_type=code&client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost
```
After authorizing, you'll be redirected to a URL like `http://localhost?code=VERIFICATION_CODE`. Copy the `code` value.
 
### 3. Exchange code for access token
```bash
curl -X POST https://launchpad.37signals.com/authorization/token \
  -d "type=web_server" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "redirect_uri=http://localhost" \
  -d "code=VERIFICATION_CODE"
```
Save the `access_token` and `refresh_token` from the response. Tokens expire after **2 weeks** — use the refresh flow below to renew.
 
### 4. Get your account ID
```bash
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
     -A "MyScript (you@example.com)" \
     https://launchpad.37signals.com/authorization.json
```
Look for the object with `"product": "bc3"` in the `accounts` array. Save its `id` (your `ACCOUNT_ID`) and `href` (your API base URL, e.g. `https://3.basecampapi.com/123456789`).
 
### Refreshing an expired token
```bash
curl -X POST https://launchpad.37signals.com/authorization/token \
  -d "type=refresh" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "refresh_token=YOUR_REFRESH_TOKEN"
```
 
---
 
## Python Script: Fetch My Outstanding Tasks
 
Install dependencies:
```bash
pip install requests
```
 
Set the required environment variables before running:
```bash
export BASECAMP_ACCESS_TOKEN="your_access_token_here"
export BASECAMP_ACCOUNT_ID="123456789"
```
 
```python
#!/usr/bin/env python3
"""
Fetch all outstanding (incomplete) Basecamp todos assigned to me.
 
Required environment variables:
  BASECAMP_ACCESS_TOKEN  — OAuth2 bearer token
  BASECAMP_ACCOUNT_ID    — numeric Basecamp account ID
"""
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
import re
import sys
import json
import requests
from pathlib import Path
 
# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------
 
ENV_PATH = Path(__file__).parent / ".env"
 
def load_env(path: Path) -> dict:
    """Parse a simple KEY="VALUE" or KEY=VALUE .env file."""
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
 
def save_env(path: Path, env: dict) -> None:
    """Write the env dict back to the .env file preserving order."""
    lines = []
    for k, v in env.items():
        lines.append(f'{k}="{v}"')
    path.write_text("\n".join(lines) + "\n")
 
# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------
 
def refresh_access_token(env: dict) -> str:
    """
    Use the refresh token to get a new access token.
    Updates env in-place and writes it back to disk.
    Raises SystemExit if required keys are missing or the request fails.
    """
    refresh_token  = env.get("BASECAMP_REFRESH_TOKEN")
    client_id      = env.get("BASECAMP_CLIENT_ID")
    client_secret  = env.get("BASECAMP_CLIENT_SECRET")
 
    missing = [k for k, v in {
        "BASECAMP_REFRESH_TOKEN": refresh_token,
        "BASECAMP_CLIENT_ID":     client_id,
        "BASECAMP_CLIENT_SECRET": client_secret,
    }.items() if not v or v.startswith("your_")]
 
    if missing:
        sys.exit(
            f"Access token is expired/invalid and auto-refresh is not configured.\n"
            f"Add these to your .env file: {', '.join(missing)}"
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
    )
 
    if resp.status_code != 200:
        sys.exit(f"Token refresh failed ({resp.status_code}): {resp.text}")
 
    data = resp.json()
    new_access_token  = data.get("access_token")
    new_refresh_token = data.get("refresh_token")  # Basecamp may rotate this
 
    if not new_access_token:
        sys.exit(f"Token refresh response missing access_token: {data}")
 
    env["BASECAMP_ACCESS_TOKEN"] = new_access_token
    if new_refresh_token:
        env["BASECAMP_REFRESH_TOKEN"] = new_refresh_token
 
    save_env(ENV_PATH, env)
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
 
def paginate(url: str, headers: dict):
    """Yield all items from a paginated Basecamp endpoint."""
    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        yield from resp.json()
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url
 
def get_my_person_id(base_url: str, headers: dict) -> int:
    resp = requests.get(f"{base_url}/my/profile.json", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    print(f"Logged in as: {data['name']}", file=sys.stderr)
    return data["id"]
 
def get_projects(base_url: str, headers: dict):
    return list(paginate(f"{base_url}/projects.json", headers))
 
def get_todolists(base_url: str, project_id, todoset_id, headers: dict):
    url = f"{base_url}/buckets/{project_id}/todosets/{todoset_id}/todolists.json"
    return list(paginate(url, headers))
 
def get_todos(base_url: str, project_id, todolist_id, headers: dict):
    url = f"{base_url}/buckets/{project_id}/todolists/{todolist_id}/todos.json"
    return list(paginate(url, headers))
 
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
 
def main():
    env = load_env(ENV_PATH)
 
    access_token = env.get("BASECAMP_ACCESS_TOKEN")
    account_id   = env.get("BASECAMP_ACCOUNT_ID")
 
    if not access_token or not account_id:
        sys.exit(
            "Error: BASECAMP_ACCESS_TOKEN and BASECAMP_ACCOUNT_ID must be set in .env"
        )
 
    base_url = f"https://3.basecampapi.com/{account_id}"
    headers  = make_headers(access_token)
 
    # --- Try once; if 401, refresh and retry ---
    try:
        my_id = get_my_person_id(base_url, headers)
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            access_token = refresh_access_token(env)
            headers      = make_headers(access_token)
            my_id        = get_my_person_id(base_url, headers)
        else:
            raise
 
    my_todos = []
    projects = get_projects(base_url, headers)
 
    for project in projects:
        project_name = project["name"]
        project_id   = project["id"]
        todoset = next(
            (d for d in project.get("dock", []) if d["name"] == "todoset"), None
        )
        if not todoset:
            continue
        todoset_id = todoset["id"]
        todolists  = get_todolists(base_url, project_id, todoset_id, headers)
        for tl in todolists:
            todos = get_todos(base_url, project_id, tl["id"], headers)
            for todo in todos:
                assignees = [a["id"] for a in todo.get("assignees", [])]
                if my_id in assignees:
                    my_todos.append({
                        "project": project_name,
                        "list":    tl["title"],
                        "task":    todo["content"],
                        "due":     todo.get("due_on"),
                        "url":     todo["app_url"],
                    })
 
    print(json.dumps(my_todos))
 
if __name__ == "__main__":
    main()
 
```
 
---
 
## How It Works
 
1. **Authenticates** using your Bearer token.
2. **Identifies you** via `GET /my/profile.json` to get your person ID.
3. **Lists all projects** via `GET /projects.json`.
4. **Finds each project's todoset** from the project's `dock` array.
5. **Iterates all todo lists** in each todoset.
6. **Fetches todos** from each list and filters for ones where your ID appears in `assignees`.
7. **Prints** the task name, project, list, due date, and a direct link.
 
Pagination is handled automatically via the `Link` response header.
 
---
 
## Key API Reference
 
| Action | Endpoint |
|--------|----------|
| My profile | `GET /my/profile.json` |
| All projects | `GET /projects.json` |
| Todo lists in a todoset | `GET /buckets/{project_id}/todosets/{todoset_id}/todolists.json` |
| Todos in a list | `GET /buckets/{project_id}/todolists/{todolist_id}/todos.json` |
| Completed todos | `GET /buckets/{project_id}/todolists/{todolist_id}/todos.json?completed=true` |
 
All requests require:
- `Authorization: Bearer YOUR_ACCESS_TOKEN`
- `User-Agent: AppName (contact@email.com)` (required by Basecamp)
 
---
 
## Sources
- [Basecamp API Todos docs](https://github.com/basecamp/bc3-api/blob/master/sections/todos.md)
- [Basecamp API Todo Lists docs](https://github.com/basecamp/bc3-api/blob/master/sections/todolists.md)
- [Basecamp API Authentication docs](https://github.com/basecamp/bc3-api/blob/master/sections/authentication.md)
- [bc3-api GitHub repo](https://github.com/basecamp/bc3-api)