#!/usr/bin/env python3
"""
Fetch all OUTSTANDING (not yet completed) Basecamp todos for a specific
person on your team.

basecamp_fetch.py's /my/assignments.json only reports the authenticated
user's own open work, so it can't answer "what does Jane still owe?" for
anyone but the token owner. This script uses the same Recordings API
sweep as basecamp_completed.py instead:

    GET /projects/recordings.json?type=Todo[&status=archived]

filtered client-side on `completed == False`, `assignees` and `due_on`.

A task's due date has no relationship to when it was created, so it can't
be used to stop pagination early the way basecamp_completed.py stops at
--since. Instead, the crawl is bounded by --lookback-months (default 6):
recordings are paged newest-created-first and the scan stops once a page
is entirely older than that cutoff, same short-circuit basecamp_completed.py
uses for --since. That means a still-open to-do created more than six
months ago won't show up in the default report — acceptable for a
"what's currently outstanding" report, and a lot faster than paging
through an account's entire history. Pass --lookback-months 0 for an
unbounded full scan.

Credentials, token-refresh logic, and person-lookup/roster helpers are
shared with basecamp_fetch.py and basecamp_completed.py.

Examples
--------
  # Who's on the account?
  ./basecamp_outstanding.py --list-people

  # Everything still open and assigned to Jane
  ./basecamp_outstanding.py --user "Jane Doe"

  # Just what's overdue, as a markdown report
  ./basecamp_outstanding.py --user jane@utsa.edu --overdue-only \
      --format markdown -o outstanding-jane.md

  # Due in the next month, one project only
  ./basecamp_outstanding.py --user "Jane" --project "Website Redesign" \
      --until 2026-10-08
"""

import sys
import csv
import json
import signal
import logging
import calendar
import argparse
import requests
from io import StringIO
from pathlib import Path
from datetime import date
from collections import OrderedDict

# Reuse the .env / token plumbing from the existing fetcher, and the
# roster/lookup/pagination helpers already built for the completed-tasks
# report rather than re-implementing them.
sys.path.insert(0, str(Path(__file__).parent))
from basecamp_fetch import (  # noqa: E402
    ENV_PATH,
    load_env,
    make_headers,
    refresh_access_token,
)
from basecamp_completed import (  # noqa: E402
    get_json,
    get_people,
    _norm,
    _tokens,
    resolve_person,
    get_archived_project_ids,
    iter_todo_recordings,
    parse_day,
    iso_day,
    fmt_span,
)

log = logging.getLogger("basecamp-outstanding")


# ---------------------------------------------------------------------------
# Shaping / filtering
# ---------------------------------------------------------------------------

def shape_todo(todo: dict) -> dict:
    """Flatten a recording into a report row."""
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
                             "archived wholesale while to-dos inside them stay incomplete. "
                             "Trashed to-dos are always excluded.")
    parser.add_argument("--lookback-months", type=int, default=6,
                        help="Stop scanning once to-dos were created more than this many "
                             "months ago (default: 6). A still-open to-do created earlier "
                             "than that won't be found. Use 0 to scan the full account "
                             "history instead.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug logging to stderr")
    parser.add_argument("--timeout", type=int, default=900,
                        help="Global script timeout in seconds (default: 900, 0=none)")
    args = parser.parse_args()

    if not args.user and args.list_people is None:
        parser.error("one of --user or --list-people is required")

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

    since = parse_day(args.since, "--since") if args.since else None
    until = parse_day(args.until, "--until") if args.until else None
    if since and until and since > until:
        sys.exit("--since must be on or before --until")
    as_of = parse_day(args.as_of, "--as-of") if args.as_of else date.today()

    env          = load_env(ENV_PATH)
    access_token = env.get("BASECAMP_ACCESS_TOKEN")
    account_id   = env.get("BASECAMP_ACCOUNT_ID")
    if not access_token or not account_id:
        sys.exit("Error: BASECAMP_ACCESS_TOKEN and BASECAMP_ACCOUNT_ID must be set in .env")

    base_url = f"https://3.basecampapi.com/{account_id}"
    headers  = make_headers(access_token)

    # Validate the token up front so a refresh happens once, not mid-crawl.
    try:
        me = get_json(f"{base_url}/my/profile.json", headers)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            headers = make_headers(refresh_access_token(env))
            me = get_json(f"{base_url}/my/profile.json", headers)
        else:
            raise
    log.debug("Authenticated as %s", me.get("name"))

    people = get_people(base_url, headers)

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
    statuses = ("active", "archived") if args.include_archived else ("active",)

    cutoff = months_ago(as_of, args.lookback_months) if args.lookback_months > 0 else None

    print(f"Streaming to-do recordings (status: {', '.join(statuses)}, "
          f"sorted created_at desc)...", file=sys.stderr)
    if cutoff:
        print(f"Will stop paginating once to-dos were created before "
              f"{cutoff.isoformat()} ({args.lookback_months} month(s) back; "
              f"pass --lookback-months 0 to scan everything).", file=sys.stderr)
    else:
        print("--lookback-months 0: paging through the full account history.",
              file=sys.stderr)

    recordings = iter_todo_recordings(base_url, headers, statuses, sort="created_at",
                                      stop_before=cutoff, hard_stop=bool(cutoff))

    if args.include_archived:
        archived_ids = get_archived_project_ids(base_url, headers)
        if archived_ids:
            print(f"Also scanning {len(archived_ids)} archived project(s)...",
                  file=sys.stderr)
            recordings += iter_todo_recordings(base_url, headers, statuses,
                                               buckets=archived_ids, sort="created_at",
                                               stop_before=cutoff, hard_stop=bool(cutoff))

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
        if not matches_person(row, person["id"]):
            continue
        if args.project and args.project.lower() not in row["project"].lower():
            continue
        todos.append(row)

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
