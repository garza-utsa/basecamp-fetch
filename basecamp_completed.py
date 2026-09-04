#!/usr/bin/env python3
"""
Fetch all COMPLETED Basecamp todos for a specific person on your team.

Basecamp's /my/assignments.json only reports the authenticated user's own
open work, and /reports/todos/assigned/{id}.json returns only *pending*
to-dos, so neither can answer "what has this person finished?".

This script uses the Recordings API instead:

    GET /projects/recordings.json?type=Todo[&status=archived]

one paginated stream covering every project the token can see, filtered
client-side on `completed`, `assignees` and `completion.created_at`. The
roster for --user comes from /reports/todos/assigned.json, which is exactly
the set of people who can have to-dos assigned.

Credentials and token-refresh logic are shared with basecamp_fetch.py
(same .env file, same auto-refresh-on-401 behavior).

Examples
--------
  # Who's on the account?
  ./basecamp_completed.py --list-people

  # Everything Jane has ever finished
  ./basecamp_completed.py --user "Jane Doe"

  # Last quarter only, as a markdown report
  ./basecamp_completed.py --user jane@utsa.edu \
      --since 2026-06-01 --until 2026-08-31 \
      --format markdown -o completed-jane-q3.md

  # One project, as CSV for a spreadsheet
  ./basecamp_completed.py --user "Jane" --project "Website Redesign" --format csv
"""

import os
import re
import sys
import csv
import json
import signal
import logging
import argparse
import requests
from io import StringIO
from pathlib import Path
from datetime import date, datetime
from collections import OrderedDict

# Reuse the .env / token plumbing from the existing fetcher.
sys.path.insert(0, str(Path(__file__).parent))
from basecamp_fetch import (  # noqa: E402
    ENV_PATH,
    TIMEOUT,
    load_env,
    make_headers,
    paginate,
    refresh_access_token,
)

log = logging.getLogger("basecamp-completed")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_json(url: str, headers: dict, params: dict = None):
    resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_people(base_url: str, headers: dict) -> list:
    """People who can have to-dos assigned to them.

    /reports/todos/assigned.json is the right roster for this job — it is
    exactly the set of assignable people, account-wide. /people.json is the
    fallback for tokens that can't see the reports endpoint.
    """
    try:
        people = list(paginate(f"{base_url}/reports/todos/assigned.json", headers))
        if people:
            log.debug("Roster from /reports/todos/assigned.json (%d people)", len(people))
            return people
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        log.debug("reports/todos/assigned.json unavailable (HTTP %s); using /people.json", status)
    return list(paginate(f"{base_url}/people.json", headers))


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _tokens(value: str) -> set:
    """Lowercase word tokens: 'Pamela A. Dyer' / 'pamela.dyer' -> {pamela, a, dyer}."""
    return {t for t in re.split(r"[^a-z0-9]+", _norm(value)) if t}


def _describe(p: dict) -> str:
    return f"{p.get('name', '?')} <{p.get('email_address') or 'email hidden'}>  (id {p['id']})"


def _ambiguous(query: str, matches: list) -> None:
    roster = "\n  ".join(_describe(m) for m in matches)
    sys.exit(f"{query!r} matches several people — use the numeric id instead:\n  {roster}")


def resolve_person(people: list, query: str) -> dict:
    """Match a person by numeric id, email, or name — in that order of confidence.

    Basecamp only returns `email_address` to account admins/owners; for other
    tokens the field comes back null for everyone but yourself. So an email
    query also falls back to matching its local part against people's names
    ('pamela.dyer@utsa.edu' -> tokens {pamela, dyer} -> 'Pamela Dyer').
    """
    q = _norm(query)
    if not q:
        sys.exit("--user was empty.")

    # 1. Numeric Basecamp person id — always unambiguous.
    if q.isdigit():
        for p in people:
            if str(p["id"]) == q:
                return p
        sys.exit(f"No person on this account with id {q}. Try --list-people.")

    # 2. Exact email or exact full name.
    exact = [p for p in people
             if _norm(p.get("email_address")) == q or _norm(p.get("name")) == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _ambiguous(query, exact)

    # 3. Substring of name or email.
    partial = [p for p in people
               if q in _norm(p.get("name")) or q in _norm(p.get("email_address"))]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        _ambiguous(query, partial)

    # 4. Token match. For an email, use the local part ('first.last@dom' -> first, last).
    q_tokens = _tokens(q.split("@")[0] if "@" in q else q)
    q_tokens.discard("utsa")
    if q_tokens:
        token_hits = [p for p in people if q_tokens <= _tokens(p.get("name"))]
        if len(token_hits) == 1:
            hit = token_hits[0]
            print(f"Note: matched {query!r} to {hit['name']} by name.", file=sys.stderr)
            return hit
        if len(token_hits) > 1:
            _ambiguous(query, token_hits)

        # 5. Last resort: any single token overlap, reported as a suggestion only.
        loose = [p for p in people if q_tokens & _tokens(p.get("name"))]
    else:
        loose = []

    # Nothing matched — explain why, including the email-visibility caveat.
    with_email = sum(1 for p in people if p.get("email_address"))
    msg = [f"No person matched {query!r}.",
           f"  {len(people)} people visible; {with_email} expose an email address to this token."]
    if "@" in q and with_email <= 1:
        msg.append("  Basecamp hides email_address from non-admin tokens, so email lookups")
        msg.append("  cannot work here — match by name or numeric id instead.")
    if loose:
        msg.append("  Did you mean:")
        msg += [f"    {_describe(p)}" for p in loose[:10]]
    else:
        msg.append("  Run --list-people to see the roster.")
    sys.exit("\n".join(msg))


def get_archived_project_ids(base_url: str, headers: dict) -> list:
    """Ids of archived projects.

    recordings.json defaults its `bucket` scope to *active projects visible to
    the user*, so `status=archived` alone still misses everything inside a
    project that was archived wholesale. Those ids have to be passed explicitly.
    """
    try:
        ids = [p["id"] for p in paginate(f"{base_url}/projects.json?status=archived", headers)]
        log.debug("%d archived project(s)", len(ids))
        return ids
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        log.warning("Could not list archived projects (HTTP %s)", status)
        return []


def paginate_pages(url: str, headers: dict):
    """Like paginate(), but yields whole pages so progress can be reported."""
    page = 1
    while url:
        log.debug("  GET %s", url)
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        yield page, resp.json()
        next_url = None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        url = next_url
        page += 1


def iter_todo_recordings(base_url: str, headers: dict, statuses: tuple,
                         buckets: list = None, sort: str = "created_at",
                         stop_before: date = None, hard_stop: bool = True) -> list:
    """Every to-do visible to this token, via the Recordings API.

    One paginated stream per status replaces the old
    projects -> todoset -> todolists -> todos crawl, which cost roughly
    (1 + projects + todolists) requests and bumped the documented rate limit
    of 50 requests per 10 seconds. Basecamp's geared pagination returns
    15/30/50/100 records per page, so a few thousand to-dos is a few dozen
    requests total.

    Results come back newest-first on `sort`, and each page reports the date
    range it covered so a long crawl shows how far back it has reached.

    `buckets` scopes the query to specific project ids; omit it for the
    default scope of all active projects.

    `stop_before` marks the start of the reporting window. With
    hard_stop=True the stream ends once a page is entirely older than that
    date — only valid with sort="updated_at", because completing a to-do
    bumps updated_at, so anything completed on/after the date must still have
    updated_at on/after it. With sort="created_at" an old to-do can be
    completed yesterday, so pass hard_stop=False there and the out-of-window
    pages are merely counted and reported.
    """
    scope = f"&bucket={','.join(str(b) for b in buckets)}" if buckets else ""
    where = f" bucket={len(buckets)} project(s)" if buckets else ""
    todos = []
    outside = 0

    for status in statuses:
        url = (f"{base_url}/projects/recordings.json?type=Todo&status={status}"
               f"&sort={sort}&direction=desc{scope}")
        stream, stopped = [], False

        for page, items in paginate_pages(url, headers):
            if not items:
                continue
            newest, oldest = day_span(i.get(sort) for i in items)

            # A page wholly older than the window start.
            if stop_before and newest and newest < stop_before:
                outside += 1
                if hard_stop:
                    # Discard rather than keep it, so the reported span stays
                    # honest about what actually got scanned.
                    print(f"  page {page:>2} ({status}{where}): reached "
                          f"{newest.isoformat()}, wholly older than "
                          f"{stop_before.isoformat()} — stopping this stream.",
                          file=sys.stderr)
                    stopped = True
                    break

            stream.extend(items)
            print(f"  page {page:>2} ({status}{where}): {len(items):>3} to-dos, "
                  f"{sort} {fmt_span(newest, oldest)} "
                  f"[{len(stream)} so far]", file=sys.stderr)

        newest, oldest = day_span(i.get(sort) for i in stream)
        print(f"  status={status}{where}: {len(stream)} to-dos, "
              f"{sort} span {fmt_span(newest, oldest)}"
              f"{' (stopped early)' if stopped else ''}", file=sys.stderr)
        todos.extend(stream)

    if outside and not hard_stop:
        print(f"  note: {outside} page(s) were entirely older than "
              f"{stop_before.isoformat()} and scanned anyway "
              f"(--full-scan is on).", file=sys.stderr)

    return todos


def shape_todo(todo: dict) -> dict:
    """Flatten a recording into a report row."""
    completion = todo.get("completion") or {}
    return {
        "project":      (todo.get("bucket") or {}).get("name", "Unknown"),
        "list":         (todo.get("parent") or {}).get("title", "Unknown"),
        "task":         todo.get("content", ""),
        "due":          todo.get("due_on"),
        "completed_at": completion.get("created_at") or todo.get("updated_at"),
        "completed_by": (completion.get("creator") or {}).get("name"),
        "url":          todo.get("app_url"),
        "_assignee_ids":  [a.get("id") for a in todo.get("assignees", [])],
        "_completer_id":  (completion.get("creator") or {}).get("id"),
    }


def matches_person(row: dict, person_id: int, mode: str) -> bool:
    if mode == "completer":
        return row["_completer_id"] == person_id
    if mode == "either":
        return person_id in row["_assignee_ids"] or row["_completer_id"] == person_id
    return person_id in row["_assignee_ids"]


def parse_day(value: str, flag: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"{flag} must be YYYY-MM-DD (got {value!r})")


def iso_day(stamp: str):
    """'2026-03-01T10:00:00.000Z' -> date(2026, 3, 1); None if unparseable."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def completed_day(todo: dict):
    return iso_day(todo.get("completed_at"))


def day_span(stamps) -> tuple:
    """(newest, oldest) dates from an iterable of ISO stamps, ignoring blanks."""
    days = [d for d in (iso_day(s) for s in stamps) if d]
    return (max(days), min(days)) if days else (None, None)


def fmt_span(newest, oldest) -> str:
    if not newest:
        return "no dates"
    if newest == oldest:
        return newest.isoformat()
    return f"{newest.isoformat()} -> {oldest.isoformat()}"


def apply_date_filter(todos: list, since: date, until: date) -> list:
    if not since and not until:
        return todos
    kept = []
    for todo in todos:
        day = completed_day(todo)
        if day is None:
            continue
        if since and day < since:
            continue
        if until and day > until:
            continue
        kept.append(todo)
    return kept


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_markdown(todos: list, person: dict, since: date, until: date) -> str:
    newest, oldest = day_span(t.get("completed_at") for t in todos)
    groups = OrderedDict()
    for todo in todos:
        groups.setdefault((todo["project"], todo["list"]), []).append(todo)

    span = "all time"
    if since and until:
        span = f"{since.isoformat()} to {until.isoformat()}"
    elif since:
        span = f"since {since.isoformat()}"
    elif until:
        span = f"through {until.isoformat()}"

    lines = [
        "---",
        "type: reference",
        f"date: {date.today().isoformat()}",
        f"person: {person['name']}",
        f"range: {span}",
        f"completed_span: {fmt_span(newest, oldest)}",
        "tags:",
        "  - type/reference",
        "  - source/basecamp",
        "  - topic/completed-tasks",
        "---",
        "",
        f"# Completed Basecamp Tasks — {person['name']}",
        "",
        f"{len(todos)} completed task{'s' if len(todos) != 1 else ''} ({span}). "
        f"Latest completion: {newest.isoformat() if newest else 'n/a'}; "
        f"earliest: {oldest.isoformat() if oldest else 'n/a'}.",
    ]

    current_project = None
    for (project, lst), items in groups.items():
        if project != current_project:
            lines.append(f"\n## {project}")
            current_project = project
        lines.append(f"\n### {lst}")
        for todo in items:
            entry = f"- [x] [{todo['task']}]({todo['url']})"
            day = completed_day(todo)
            if day:
                entry += f" — completed {day.isoformat()}"
            if todo.get("due"):
                entry += f" (due {todo['due']})"
            lines.append(entry)

    if not groups:
        lines.append("\n_No completed tasks found for this person in this range._")

    return "\n".join(lines) + "\n"


def format_csv(todos: list) -> str:
    buf = StringIO()
    fields = ["project", "list", "task", "due", "completed_at", "completed_by", "url"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(todos)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch completed Basecamp todos for a specific team member.",
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
    parser.add_argument("--since", help="Only tasks completed on/after this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only tasks completed on/before this date (YYYY-MM-DD)")
    parser.add_argument("--format", choices=["json", "markdown", "csv"], default="json")
    parser.add_argument("--output", "-o", action="append",
                        help="Write output to file(s) instead of stdout (repeatable)")
    parser.add_argument("--by", choices=["assignee", "completer", "either"],
                        default="assignee",
                        help="assignee: to-dos assigned to them that are done (default). "
                             "completer: to-dos they personally checked off. "
                             "either: the union of both.")
    parser.add_argument("--full-scan", action="store_true",
                        help="Page through every to-do in the account instead of "
                             "stopping once pagination passes --since. Slower, but "
                             "relies on no assumption about updated_at. Use it once "
                             "to confirm a report is complete.")
    parser.add_argument("--include-archived", action="store_true",
                        help="Also scan archived to-dos and archived projects "
                             "(recommended for reports covering a past year). "
                             "Trashed to-dos are always excluded.")
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
          f"(id {person['id']}) — matching by {args.by}", file=sys.stderr)

    # Trashed to-dos are deliberately excluded — deleted work is not completed work.
    statuses = ("active", "archived") if args.include_archived else ("active",)

    # Stop paginating once the stream passes --since. Sorting by updated_at makes
    # this safe: completing a to-do bumps updated_at, so anything completed on or
    # after --since still has updated_at on or after it. Without --since there is
    # no window to stop at; with --full-scan the user has asked for everything.
    hard_stop = bool(since) and not args.full_scan
    sort = "updated_at" if hard_stop else "created_at"
    stop_before = since

    print(f"Streaming to-do recordings (status: {', '.join(statuses)}, "
          f"sorted {sort} desc)...", file=sys.stderr)
    if hard_stop:
        print(f"Will stop paginating at {since.isoformat()} "
              f"(pass --full-scan to scan everything).", file=sys.stderr)
    elif since:
        print("--full-scan: paging through every to-do regardless of --since.",
              file=sys.stderr)

    recordings = iter_todo_recordings(base_url, headers, statuses,
                                      sort=sort, stop_before=stop_before,
                                      hard_stop=hard_stop)

    if args.include_archived:
        archived_ids = get_archived_project_ids(base_url, headers)
        if archived_ids:
            print(f"Also scanning {len(archived_ids)} archived project(s)...",
                  file=sys.stderr)
            recordings += iter_todo_recordings(base_url, headers, statuses,
                                               archived_ids, sort, stop_before,
                                               hard_stop)

    # A to-do can arrive twice once bucket-scoped passes overlap the default scope.
    unique = {}
    for rec in recordings:
        unique.setdefault(rec.get("id"), rec)

    scan_newest, scan_oldest = day_span(r.get(sort) for r in unique.values())
    print(f"Scanned {len(unique)} to-do(s); {sort} span "
          f"{fmt_span(scan_newest, scan_oldest)}", file=sys.stderr)

    todos = []
    for rec in unique.values():
        if not rec.get("completed"):
            continue
        row = shape_todo(rec)
        if not matches_person(row, person["id"], args.by):
            continue
        if args.project and args.project.lower() not in row["project"].lower():
            continue
        todos.append(row)

    todos = apply_date_filter(todos, since, until)
    for row in todos:
        row.pop("_assignee_ids", None)
        row.pop("_completer_id", None)
    todos.sort(key=lambda t: (t.get("completed_at") or "", t["project"], t["task"]),
               reverse=True)
    done_newest, done_oldest = day_span(t.get("completed_at") for t in todos)
    print(f"Found {len(todos)} completed task(s); completed "
          f"{fmt_span(done_newest, done_oldest)}", file=sys.stderr)

    if args.format == "markdown":
        output = format_markdown(todos, person, since, until)
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
