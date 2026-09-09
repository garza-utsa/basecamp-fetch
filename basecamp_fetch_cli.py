#!/usr/bin/env python3
"""
Fetch all outstanding (incomplete) Basecamp todos assigned to me.

This is a basecamp-cli-backed version of basecamp_fetch.py. Instead of
talking to the Basecamp REST API directly and managing OAuth tokens in a
.env file, it shells out to the `basecamp` CLI
(https://basecamp.com/agents, https://github.com/basecamp/basecamp-cli).

The CLI owns credential storage and token refresh, so this script has no
.env file and no OAuth plumbing of its own. Requires the CLI to already be
installed and authenticated:

  https://github.com/basecamp/basecamp-cli
  basecamp auth login
"""

import sys
import json
import shutil
import logging
import argparse
import subprocess
from collections import OrderedDict
from datetime import date
from pathlib import Path

log = logging.getLogger("basecamp")

BASECAMP_BIN = shutil.which("basecamp") or "basecamp"

# ---------------------------------------------------------------------------
# basecamp-cli helper
# ---------------------------------------------------------------------------

def run_cli(*args: str, timeout) -> dict:
    """Run a `basecamp` subcommand in agent mode and return the parsed payload.

    `--agent` means JSON + quiet: on success stdout is the raw data payload
    (no {ok,data} envelope); on failure it's {"ok": false, "error", "code",
    "hint"}. `--agent` also disables interactive prompts, which matters when
    running headlessly.
    """
    cmd = [BASECAMP_BIN, *args, "--agent"]
    log.debug("RUN %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        sys.exit(
            "Error: 'basecamp' CLI not found on PATH.\n"
            "Install it from https://github.com/basecamp/basecamp-cli and run "
            "`basecamp auth login`."
        )
    except subprocess.TimeoutExpired:
        sys.exit(f"ERROR: `{' '.join(cmd)}` timed out after {timeout}s")

    stdout = proc.stdout.strip()
    if not stdout:
        sys.exit(
            f"Error: `{' '.join(cmd)}` produced no output "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        sys.exit(f"Error: could not parse `{' '.join(cmd)}` output as JSON:\n{stdout}")

    if isinstance(payload, dict) and payload.get("ok") is False:
        hint = f" ({payload['hint']})" if payload.get("hint") else ""
        sys.exit(f"basecamp CLI error: {payload.get('error')}{hint}")

    return payload

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_markdown(todos: list) -> str:
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
            entry = f"- [ ] [{todo['task']}]({todo['url']})"
            if todo.get("due"):
                entry += f" — due {todo['due']}"
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

    cli_timeout = args.timeout if args.timeout else None

    me = run_cli("me", timeout=cli_timeout)
    print(f"Logged in as: {me.get('name')}", file=sys.stderr)

    data = run_cli("assignments", "list", timeout=cli_timeout)

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
