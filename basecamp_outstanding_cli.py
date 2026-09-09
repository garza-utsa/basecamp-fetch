#!/usr/bin/env python3
"""
Fetch all OUTSTANDING (not yet completed) Basecamp todos for a specific
person on your team.

This is a basecamp-cli-backed version of basecamp_outstanding.py
(https://basecamp.com/agents, https://github.com/basecamp/basecamp-cli).

The original crawled the whole account via the Recordings API
(GET /projects/recordings.json?type=Todo) and filtered client-side on
assignee, because that REST endpoint has no way to filter by person —
hence --lookback-months, which bounded the crawl so a report didn't have
to page through every to-do ever created. The basecamp CLI's
`todos list --all-projects --assignee <id>` filters by assignee
server-side, so this version asks for exactly this person's incomplete
to-dos instead of paging through everyone's. --lookback-months is kept
for interface parity, but it's now just a content filter (exclude to-dos
created earlier than N months ago), not a pagination bound.

**The one thing none of the CLI's own commands can do:** find a to-do
whose *parent todolist* (not the project) was individually archived.
Once a todolist is archived, its to-dos vanish from every CLI-level
listing regardless of --status — `todos list --status incomplete`,
`--status archived`, `recordings todos --status archived`, even the raw
`api get` passthrough (which caps at one page with no pagination
visibility) — even though `todos show <id>` proves the to-do exists,
`status: "archived"`, `inherits_status: true`. Confirmed empirically
against a real account: an item invisible to every one of those turned
up on page 14 of a 1195-recording account-wide crawl of the *raw*
`/projects/recordings.json?type=Todo&status=archived` endpoint (the same
endpoint the original REST script used), walked with real `Link`-header
pagination — which only a direct HTTP call can do; the CLI exposes no
pagination metadata for this endpoint at all. So the archived-status
sweep (only runs with --include-archived) makes that one direct HTTP
call itself, authenticated via a token from `basecamp auth token` rather
than managing OAuth refresh ourselves — everything else still goes
through the CLI.

The CLI also owns credential storage and token refresh, so there's no
.env file and no OAuth plumbing here for anything else — just `basecamp
auth login` once, up front.

Person-lookup/roster helpers are shared with basecamp_completed.py.

Examples
--------
  # Who's on the account?
  ./basecamp_outstanding_cli.py --list-people

  # Everything still open and assigned to Jane
  ./basecamp_outstanding_cli.py --user "Jane Doe"

  # Just what's overdue, as a markdown report
  ./basecamp_outstanding_cli.py --user jane@utsa.edu --overdue-only \
      --format markdown -o outstanding-jane.md

  # Due in the next month, one project only
  ./basecamp_outstanding_cli.py --user "Jane" --project "Website Redesign" \
      --until 2026-10-08
"""

import sys
import csv
import json
import shutil
import logging
import calendar
import argparse
import subprocess
import requests
from io import StringIO
from pathlib import Path
from datetime import date
from collections import OrderedDict

# Reuse the pure roster/lookup/date helpers already built for the completed-
# tasks report rather than re-implementing them — none of these talk to the
# network directly, so they're just as good in front of the CLI.
sys.path.insert(0, str(Path(__file__).parent))
from basecamp_completed import (  # noqa: E402
    _norm,
    _tokens,
    resolve_person,
    parse_day,
    iso_day,
)

log = logging.getLogger("basecamp-outstanding")

BASECAMP_BIN = shutil.which("basecamp") or "basecamp"

# Matches real to-do records (every Basecamp to-do has "content" and a
# boolean "completed") regardless of whether a listing comes back as a flat
# array or grouped/nested by project — some cross-project listings do the
# latter, and this filter recurses through either shape identically. Doesn't
# require "bucket" too: an account-wide grouped shape may hoist the project
# reference onto the group wrapper rather than repeating it per to-do, and
# shape_todo() already tolerates a missing bucket (falls back to "Unknown").
TODO_JQ = '[.. | objects | select(has("content") and has("completed"))]'


# ---------------------------------------------------------------------------
# basecamp-cli helper
# ---------------------------------------------------------------------------

def run_cli(*args: str, timeout, jq: str = None):
    """Run a `basecamp` subcommand in agent mode and return the parsed payload.

    `--agent` means JSON + quiet: on success stdout is the raw data payload
    (no {ok,data} envelope); on failure it's {"ok": false, "error", "code",
    "hint"}. `--agent` also disables interactive prompts, which matters when
    running headlessly. `--jq` (built-in, no external jq required) runs on
    that same data-only payload when combined with --agent.
    """
    cmd = [BASECAMP_BIN, *args, "--agent"]
    if jq:
        cmd += ["--jq", jq]
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


def get_people(timeout) -> list:
    """Everyone on the account — the roster --user is matched against."""
    people = run_cli("people", "list", "--all", timeout=timeout)
    return people if isinstance(people, list) else []


def get_archived_project_ids(timeout) -> list:
    """Ids of archived projects.

    `todos list --all-projects` scopes to active projects only, so a to-do
    inside a project that was archived wholesale needs its project id
    passed explicitly via --in.
    """
    projects = run_cli("projects", "list", "--status", "archived", "--all", timeout=timeout)
    if not isinstance(projects, list):
        log.warning("Could not list archived projects")
        return []
    ids = [p["id"] for p in projects]
    log.debug("%d archived project(s)", len(ids))
    return ids


def fetch_todos(person_id, status: str, timeout, in_project=None) -> list:
    """Every to-do with the given status assigned to `person_id`.

    Account-wide, --assignee is a server-side filter, exact regardless of
    account history size. Never called with status="archived" (see
    iter_archived_todo_recordings() for why that status needs a different
    mechanism entirely).
    """
    cli_args = ["todos", "list", "--assignee", str(person_id), "--status", status, "--all"]
    cli_args += ["--in", str(in_project)] if in_project else ["--all-projects"]
    todos = run_cli(*cli_args, timeout=timeout, jq=TODO_JQ)
    return todos if isinstance(todos, list) else []


def get_bearer_token(timeout) -> str:
    """A CLI-managed, auto-refreshed bearer token for the one direct HTTP
    call below. Never printed or logged — read straight into memory.
    """
    cmd = [BASECAMP_BIN, "auth", "token", "--stored"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.exit(f"Could not get an auth token from the CLI: {e}")
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        sys.exit(f"Could not get an auth token from the CLI: {proc.stderr.strip()}")
    return token


def get_account_id(timeout) -> str:
    """The account id the CLI itself resolves for every other call.

    `basecamp accounts show` returns a different, unrelated id (the
    37signals identity/organization id, not the Basecamp 3 product account
    id used in `3.basecampapi.com/{id}` URLs) — using it 404s every direct
    HTTP call below. `config show` reports the same effective account_id
    (env > local > repo > global > system > defaults) that `todos list`
    and friends resolve internally, so it's the only reliable source here.
    """
    config = run_cli("config", "show", timeout=timeout)
    account_id = (config.get("account_id") or {}).get("value") if isinstance(config, dict) else None
    if not account_id:
        sys.exit("Could not determine the account id from `basecamp config show`.")
    return str(account_id)


def iter_archived_todo_recordings(account_id: str, token: str, timeout,
                                  in_project=None) -> list:
    """Every Todo recording with status == "archived", via a direct HTTP call.

    See the module docstring: no CLI-level command can page through this —
    `todos list --status archived`, `recordings todos --status archived`,
    and `api get` (capped at one page, no Link-header visibility) all miss
    a to-do whose *todolist* was archived, even in an otherwise-active
    project. The raw endpoint does include those; it just needs the real
    Link-header pagination that only a direct HTTP call can walk. Results
    are unfiltered by assignee (this endpoint has no such param) — the
    caller applies matches_person() client-side, same as it already does
    for anything with status == "archived".

    `in_project` scopes to one bucket — used for the archived-*project*
    sweep, since this endpoint's default bucket scope is "all active
    projects" and would otherwise miss a to-do that's doubly nested (an
    archived todolist inside a wholesale-archived project).
    """
    base_url = f"https://3.basecampapi.com/{account_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "basecamp_outstanding_cli.py (archived-todo sweep)",
    }
    scope = f"&bucket={in_project}" if in_project else ""
    url = (f"{base_url}/projects/recordings.json?type=Todo&status=archived"
           f"&sort=created_at&direction=desc{scope}")
    items = []
    request_timeout = timeout or 30
    while url:
        resp = requests.get(url, headers=headers, timeout=request_timeout)
        resp.raise_for_status()
        items.extend(resp.json())
        next_url = None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url
    return items


# ---------------------------------------------------------------------------
# Shaping / filtering
# ---------------------------------------------------------------------------

def shape_todo(todo: dict) -> dict:
    """Flatten a to-do into a report row.

    Most fetches that produce these rows already passed --assignee to the
    CLI, so no client-side re-check is needed for them — except a status
    == "archived" record, which was fetched unfiltered (see fetch_todos())
    and still needs matches_person() applied in main().
    """
    return {
        "project":      (todo.get("bucket") or {}).get("name", "Unknown"),
        "list":         (todo.get("parent") or {}).get("title", "Unknown"),
        "task":         todo.get("content", ""),
        "due":          todo.get("due_on"),
        "created_at":   todo.get("created_at"),
        "assignees":    ", ".join(a.get("name", "?") for a in todo.get("assignees", [])),
        "url":          todo.get("app_url"),
        "_assignee_ids": [a.get("id") for a in todo.get("assignees", [])],
    }


def matches_person(row: dict, person_id: int) -> bool:
    return person_id in row["_assignee_ids"]


def apply_due_filter(todos: list, since: date, until: date) -> list:
    """Keep only tasks with a due date inside [since, until].

    Tasks with no due date are excluded once a window is set, same as
    basecamp_completed.py drops tasks with no completion date once --since
    or --until is set — there's nothing on them to compare against.
    """
    if not since and not until:
        return todos
    kept = []
    for todo in todos:
        day = iso_day(todo.get("due"))
        if day is None:
            continue
        if since and day < since:
            continue
        if until and day > until:
            continue
        kept.append(todo)
    return kept


def apply_lookback_filter(todos: list, cutoff: date) -> list:
    """Drop to-dos created before `cutoff` (a no-op when cutoff is None)."""
    if not cutoff:
        return todos
    kept = []
    for todo in todos:
        day = iso_day(todo.get("created_at"))
        if day is not None and day < cutoff:
            continue
        kept.append(todo)
    return kept


def due_sort_key(todo: dict):
    day = iso_day(todo.get("due"))
    return (day is None, day or date.max, todo["project"], todo["task"])


def months_ago(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_markdown(todos: list, person: dict, since: date, until: date, as_of: date) -> str:
    groups = OrderedDict()
    for todo in todos:
        groups.setdefault((todo["project"], todo["list"]), []).append(todo)

    span = "no due-date filter"
    if since and until:
        span = f"due {since.isoformat()} to {until.isoformat()}"
    elif since:
        span = f"due on/after {since.isoformat()}"
    elif until:
        span = f"due on/before {until.isoformat()}"

    overdue_n = sum(1 for t in todos if (iso_day(t.get("due")) or date.max) < as_of)
    no_due_n = sum(1 for t in todos if not t.get("due"))

    lines = [
        "---",
        "type: reference",
        f"date: {date.today().isoformat()}",
        f"person: {person['name']}",
        f"range: {span}",
        f"as_of: {as_of.isoformat()}",
        "tags:",
        "  - type/reference",
        "  - source/basecamp",
        "  - topic/outstanding-tasks",
        "---",
        "",
        f"# Outstanding Basecamp Tasks — {person['name']}",
        "",
        f"{len(todos)} outstanding task{'s' if len(todos) != 1 else ''} ({span}), "
        f"as of {as_of.isoformat()}. {overdue_n} overdue; {no_due_n} with no due date.",
    ]

    current_project = None
    for (project, lst), items in groups.items():
        if project != current_project:
            lines.append(f"\n## {project}")
            current_project = project
        lines.append(f"\n### {lst}")
        for todo in items:
            entry = f"- [ ] [{todo['task']}]({todo['url']})"
            day = iso_day(todo.get("due"))
            if day and day < as_of:
                entry += f" — **overdue**, due {day.isoformat()} ({(as_of - day).days}d)"
            elif day and day == as_of:
                entry += f" — due today ({day.isoformat()})"
            elif day:
                entry += f" — due {day.isoformat()}"
            else:
                entry += " — no due date"
            lines.append(entry)

    if not groups:
        lines.append("\n_No outstanding tasks found for this person in this range._")

    return "\n".join(lines) + "\n"


def format_csv(todos: list) -> str:
    buf = StringIO()
    fields = ["project", "list", "task", "due", "created_at", "assignees", "url"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(todos)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch outstanding (incomplete) Basecamp todos for a specific team member.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user", "-u",
                        help="Person to report on: name, email, or numeric Basecamp id")
    parser.add_argument("--list-people", nargs="?", const="", default=None,
                        metavar="FILTER",
                        help="Print the account roster and exit; optional FILTER "
                             "matches name or email substrings")
    parser.add_argument("--project", "-p",
                        help="Limit to projects whose name contains this text")
    parser.add_argument("--since", help="Only tasks due on/after this date (YYYY-MM-DD); "
                                        "tasks with no due date are excluded once this is set")
    parser.add_argument("--until", help="Only tasks due on/before this date (YYYY-MM-DD); "
                                        "tasks with no due date are excluded once this is set")
    parser.add_argument("--as-of", help="Date to compute overdue status against "
                                        "(default: today, YYYY-MM-DD)")
    parser.add_argument("--overdue-only", action="store_true",
                        help="Only include tasks whose due date is before --as-of")
    parser.add_argument("--format", choices=["json", "markdown", "csv"], default="json")
    parser.add_argument("--output", "-o", action="append",
                        help="Write output to file(s) instead of stdout (repeatable)")
    parser.add_argument("--include-archived", action="store_true",
                        help="Also scan to-dos inside archived projects — projects can be "
                             "archived wholesale while to-dos inside them stay incomplete — "
                             "and to-dos individually archived while still incomplete. "
                             "Trashed to-dos are always excluded.")
    parser.add_argument("--lookback-months", type=int, default=6,
                        help="Exclude to-dos created more than this many months ago "
                             "(default: 6). Use 0 for no created-date filter.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug logging to stderr")
    parser.add_argument("--timeout", type=int, default=900,
                        help="Per-CLI-call timeout in seconds (default: 900, 0=none)")
    args = parser.parse_args()

    if not args.user and args.list_people is None:
        parser.error("one of --user or --list-people is required")

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cli_timeout = args.timeout if args.timeout else None

    since = parse_day(args.since, "--since") if args.since else None
    until = parse_day(args.until, "--until") if args.until else None
    if since and until and since > until:
        sys.exit("--since must be on or before --until")
    as_of = parse_day(args.as_of, "--as-of") if args.as_of else date.today()

    me = run_cli("me", timeout=cli_timeout)
    log.debug("Authenticated as %s", me.get("name"))

    people = get_people(cli_timeout)

    if args.list_people is not None:
        needle = _norm(args.list_people)
        shown = [p for p in people
                 if not needle
                 or needle in _norm(p.get("name"))
                 or needle in _norm(p.get("email_address"))
                 or _tokens(needle) & _tokens(p.get("name"))]
        for p in sorted(shown, key=lambda x: (x.get("name") or "").lower()):
            flags = []
            if p.get("admin"):
                flags.append("admin")
            if p.get("owner"):
                flags.append("owner")
            if p.get("client"):
                flags.append("client")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"{p['id']:>12}  {p.get('name', '?'):<32} "
                  f"{p.get('email_address') or '(email hidden)'}{suffix}")
        with_email = sum(1 for p in people if p.get("email_address"))
        print(f"\n{len(shown)} of {len(people)} people shown; "
              f"{with_email} expose an email address to this token.", file=sys.stderr)
        if with_email <= 1:
            print("Basecamp only returns email_address to account admins/owners — "
                  "match by name or numeric id.", file=sys.stderr)
        return

    person = resolve_person(people, args.user)
    print(f"Reporting on: {person['name']} <{person.get('email_address') or 'email hidden'}> "
          f"(id {person['id']})", file=sys.stderr)

    # Trashed to-dos are deliberately excluded — deleted work isn't outstanding work.
    cutoff = months_ago(as_of, args.lookback_months) if args.lookback_months > 0 else None
    if cutoff:
        print(f"Excluding to-dos created before {cutoff.isoformat()} "
              f"({args.lookback_months} month(s) back; pass --lookback-months 0 "
              f"to include everything).", file=sys.stderr)
    else:
        print("--lookback-months 0: no created-date filter.", file=sys.stderr)

    print(f"Fetching {person['name']}'s incomplete to-dos across all projects...",
          file=sys.stderr)
    recordings = fetch_todos(person["id"], "incomplete", cli_timeout)
    print(f"  status=incomplete: {len(recordings)} to-do(s)", file=sys.stderr)

    if args.include_archived:
        # "archived" here covers a to-do whose todolist was individually
        # archived, even inside an otherwise-active project — see the
        # module docstring for why this needs a direct HTTP call instead
        # of any basecamp-cli command. These come back unfiltered by
        # assignee and still completed+incomplete mixed together; main()'s
        # existing `if rec.get("completed"): continue` and the
        # matches_person() check below handle narrowing that down.
        account_id = get_account_id(cli_timeout)
        token = get_bearer_token(cli_timeout)
        print("Fetching archived-status to-dos account-wide via direct API "
              "(works around a CLI limitation — see module docstring)...",
              file=sys.stderr)
        archived_items = iter_archived_todo_recordings(account_id, token, cli_timeout)
        print(f"  status=archived: {len(archived_items)} to-do(s) account-wide",
              file=sys.stderr)
        recordings.extend(archived_items)

        archived_ids = get_archived_project_ids(cli_timeout)
        if archived_ids:
            print(f"Also scanning {len(archived_ids)} archived project(s)...",
                  file=sys.stderr)
            for pid in archived_ids:
                # Incomplete to-dos in a wholesale-archived project — the
                # todolist-archival bug doesn't apply here, so the normal
                # CLI fetch works fine, scoped to just this one project.
                recordings.extend(
                    fetch_todos(person["id"], "incomplete", cli_timeout, in_project=pid))
                # Doubly nested case: an archived todolist inside this
                # archived project. The account-wide archived sweep above
                # defaults to active-projects-only scope, so it wouldn't
                # have caught this either.
                recordings.extend(iter_archived_todo_recordings(
                    account_id, token, cli_timeout, in_project=pid))

    # A to-do can arrive twice once bucket-scoped passes overlap the default scope.
    unique = {}
    for rec in recordings:
        unique.setdefault(rec.get("id"), rec)
    print(f"Scanned {len(unique)} to-do(s)", file=sys.stderr)

    todos = []
    for rec in unique.values():
        if rec.get("completed"):
            continue
        row = shape_todo(rec)
        # status == "archived" records were fetched unfiltered by assignee
        # (see fetch_todos()) and still need the client-side check.
        if rec.get("status") == "archived" and not matches_person(row, person["id"]):
            continue
        if args.project and args.project.lower() not in row["project"].lower():
            continue
        todos.append(row)

    todos = apply_lookback_filter(todos, cutoff)
    todos = apply_due_filter(todos, since, until)
    if args.overdue_only:
        todos = [t for t in todos if (iso_day(t.get("due")) or date.max) < as_of]

    for row in todos:
        row.pop("_assignee_ids", None)
    todos.sort(key=due_sort_key)

    overdue_n = sum(1 for t in todos if (iso_day(t.get("due")) or date.max) < as_of)
    print(f"Found {len(todos)} outstanding task(s); {overdue_n} overdue as of "
          f"{as_of.isoformat()}", file=sys.stderr)

    if args.format == "markdown":
        output = format_markdown(todos, person, since, until, as_of)
    elif args.format == "csv":
        output = format_csv(todos)
    else:
        output = json.dumps(todos, indent=2)

    if args.output:
        for path in args.output:
            try:
                Path(path).write_text(output)
                log.debug("Wrote %s", path)
            except PermissionError:
                log.warning("Permission denied writing %s", path)
    else:
        print(output)


if __name__ == "__main__":
    main()
