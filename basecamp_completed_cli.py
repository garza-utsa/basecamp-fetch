#!/usr/bin/env python3
"""
Fetch all COMPLETED Basecamp todos for a specific person on your team.

This is a basecamp-cli-backed version of basecamp_completed.py
(https://basecamp.com/agents, https://github.com/basecamp/basecamp-cli).

Basecamp's /my/assignments.json only reports the authenticated user's own
open work, and /reports/todos/assigned/{id}.json returns only *pending*
to-dos, so neither can answer "what has this person finished?". The
original solved this with an unbounded Recordings-API crawl
(GET /projects/recordings.json?type=Todo) filtered client-side on
completed/assignee/completer, bounded by --since via a hard-stop once
pagination (sorted by updated_at) passed the window — because that
endpoint has no server-side assignee filter.

The basecamp CLI's `todos list --all-projects --assignee <id> --status
completed` filters by assignee server-side, so the common case
(--by assignee, the default) now just asks for exactly this person's
completed to-dos directly — no crawl, no --since-based pagination cutoff
needed, and no risk of missing anything regardless of age.

--by completer/either still needs an account-wide scan, since "who
completed it" isn't a server-side filter on any listing. That path keeps
the original's page-by-page hard-stop-at-`--since` strategy, just walking
`basecamp todos list --page N` instead of raw recordings.json pages.

**The one thing none of the CLI's own commands can do:** find a to-do
whose *parent todolist* (not the project) was individually archived.
Once a todolist is archived, its to-dos vanish from every CLI-level
listing regardless of --status — `todos list --status completed`,
`--status archived`, `recordings todos --status archived`, even the raw
`api get` passthrough (which caps at one page with no pagination
visibility) — even though `todos show <id>` proves the to-do exists,
completed, assigned, `status: "archived"`, `inherits_status: true`.
Confirmed empirically against this account: an item invisible to every
one of those turned up on page 14 of a 1195-recording account-wide crawl
of the *raw* `/projects/recordings.json?type=Todo&status=archived`
endpoint — the same endpoint the original REST script used — walked with
real `Link`-header pagination, which only a direct HTTP call can do; the
CLI exposes no pagination metadata for this endpoint at all. So the
archived-status sweep (only runs with --include-archived) makes that one
direct HTTP call itself, authenticated via a token from `basecamp auth
token` rather than managing OAuth refresh ourselves — everything else
still goes through the CLI.

The CLI also owns credential storage and token refresh, so there's no
.env file and no OAuth plumbing here for anything else — just `basecamp
auth login` once, up front.

Person-lookup, date, and output-shaping helpers are pure functions with
no REST dependency of their own, so they're reused directly from
basecamp_completed.py rather than reimplemented.

Examples
--------
  # Who's on the account?
  ./basecamp_completed_cli.py --list-people

  # Everything Jane has ever finished
  ./basecamp_completed_cli.py --user "Jane Doe"

  # Last quarter only, as a markdown report
  ./basecamp_completed_cli.py --user jane@utsa.edu \
      --since 2026-06-01 --until 2026-08-31 \
      --format markdown -o completed-jane-q3.md

  # One project, as CSV for a spreadsheet
  ./basecamp_completed_cli.py --user "Jane" --project "Website Redesign" --format csv
"""

import sys
import json
import shutil
import logging
import argparse
import subprocess
import requests
from pathlib import Path
from datetime import date

# Reuse the pure roster/lookup/date/shaping helpers already built for the
# REST version — none of these talk to the network directly, so they're
# just as good in front of the CLI.
sys.path.insert(0, str(Path(__file__).parent))
from basecamp_completed import (  # noqa: E402
    _norm,
    _tokens,
    resolve_person,
    parse_day,
    day_span,
    fmt_span,
    shape_todo,
    matches_person,
    apply_date_filter,
    format_markdown,
    format_csv,
)

log = logging.getLogger("basecamp-completed")

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

def run_cli(*args: str, timeout, jq: str = None, fatal: bool = True):
    """Run a `basecamp` subcommand in agent mode and return the parsed payload.

    `--agent` means JSON + quiet: on success stdout is the raw data payload
    (no {ok,data} envelope); on failure it's {"ok": false, "error", "code",
    "hint"}. `--agent` also disables interactive prompts. `--jq` (built-in,
    no external jq required) runs on that same data-only payload when
    combined with --agent.

    With fatal=False an {"ok": false, ...} response is returned as-is
    instead of exiting — used by the pager below, which can't tell in
    advance whether the CLI signals "past the last page" with an empty
    list or with an error.
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
        if not fatal:
            return payload
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


def fetch_todos_by_assignee(person_id, status: str, timeout, in_project=None) -> list:
    """Every to-do with the given status assigned to `person_id`.

    Account-wide, --assignee is a server-side filter, so this is a single
    exact fetch — no pagination bookkeeping needed regardless of account
    history size. Never called with status="archived" (see
    iter_archived_todo_recordings() for why that status needs a different
    mechanism entirely).
    """
    cli_args = ["todos", "list", "--assignee", str(person_id), "--status", status, "--all"]
    cli_args += ["--in", str(in_project)] if in_project else ["--all-projects"]
    todos = run_cli(*cli_args, timeout=timeout, jq=TODO_JQ)
    return todos if isinstance(todos, list) else []


def fetch_completed_unfiltered(timeout, in_project) -> list:
    """Completed to-dos in one project, regardless of assignee.

    Only used for the archived-project sweep under --by completer/either,
    where the scope (one project's history) is already small enough for a
    plain --all fetch.
    """
    todos = run_cli("todos", "list", "--status", "completed", "--in", str(in_project),
                    "--all", timeout=timeout, jq=TODO_JQ)
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
        "User-Agent": "basecamp_completed_cli.py (archived-todo sweep)",
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


def cli_page_stream(cli_args: list, timeout):
    """Yield (page_number, items) for an account-wide todos-list query.

    Walks `--page 1`, `--page 2`, ... until a page comes back empty (or
    erroring, treated the same way past page 1 — the CLI's own way of
    saying "nothing more"). A page-1 error is left fatal since that's a
    real problem (bad flags, auth), not the end of a stream.
    """
    page = 1
    while True:
        result = run_cli(*cli_args, "--page", str(page), timeout=timeout, jq=TODO_JQ,
                         fatal=(page == 1))
        if isinstance(result, dict) and result.get("ok") is False:
            log.debug("Stopping pagination at page %d: %s", page, result.get("error"))
            return
        if not isinstance(result, list) or not result:
            return
        yield page, result
        page += 1


def iter_todo_recordings(statuses: tuple, sort: str, timeout,
                         stop_before: date = None, hard_stop: bool = True) -> list:
    """Every to-do account-wide, regardless of assignee, via CLI pagination.

    Ported from basecamp_completed.py's iter_todo_recordings(): same
    hard-stop-at-stop_before strategy (results come back newest-first on
    `sort`, and pagination stops once a page is entirely older than the
    window — only safe with sort="updated", since completing a to-do bumps
    updated_at). Only used for --by completer/either, where "who completed
    it" can't be filtered server-side and the crawl has to see everything.
    Never called with "archived" in `statuses` — see
    iter_archived_todo_recordings() for why that status needs a completely
    different mechanism.
    """
    field = "updated_at" if sort == "updated" else "created_at"
    todos = []
    outside = 0

    for status in statuses:
        cli_args = ["todos", "list", "--status", status, "--all-projects",
                    "--sort", sort, "--reverse"]
        stream, stopped = [], False

        for page, items in cli_page_stream(cli_args, timeout):
            newest, oldest = day_span(i.get(field) for i in items)

            if stop_before and newest and newest < stop_before:
                outside += 1
                if hard_stop:
                    print(f"  page {page:>2} ({status}): reached "
                          f"{newest.isoformat()}, wholly older than "
                          f"{stop_before.isoformat()} — stopping this stream.",
                          file=sys.stderr)
                    stopped = True
                    break

            stream.extend(items)
            print(f"  page {page:>2} ({status}): {len(items):>3} to-dos, "
                  f"{field} {fmt_span(newest, oldest)} "
                  f"[{len(stream)} so far]", file=sys.stderr)

        newest, oldest = day_span(i.get(field) for i in stream)
        print(f"  status={status}: {len(stream)} to-dos, "
              f"{field} span {fmt_span(newest, oldest)}"
              f"{' (stopped early)' if stopped else ''}", file=sys.stderr)
        todos.extend(stream)

    if outside and not hard_stop:
        print(f"  note: {outside} page(s) were entirely older than "
              f"{stop_before.isoformat()} and scanned anyway "
              f"(--full-scan is on).", file=sys.stderr)

    return todos


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
                        help="assignee: to-dos assigned to them that are done (default, "
                             "server-side filtered, exact regardless of age). "
                             "completer: to-dos they personally checked off (requires an "
                             "account-wide scan). either: the union of both.")
    parser.add_argument("--full-scan", action="store_true",
                        help="With --by completer/either: page through every to-do in "
                             "the account instead of stopping once pagination passes "
                             "--since. Slower, but relies on no assumption about "
                             "updated_at. Has no effect with --by assignee, which is "
                             "always exact. Use it once to confirm a report is complete.")
    parser.add_argument("--include-archived", action="store_true",
                        help="Also scan archived to-dos and archived projects "
                             "(recommended for reports covering a past year). "
                             "Trashed to-dos are always excluded.")
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
          f"(id {person['id']}) — matching by {args.by}", file=sys.stderr)

    # Trashed to-dos are deliberately excluded — deleted work is not completed work.
    if args.by == "assignee":
        if args.full_scan:
            print("--full-scan has no effect with --by assignee: assignee filtering "
                  "is already server-side and exact, not a bounded crawl.",
                  file=sys.stderr)
        print(f"Fetching {person['name']}'s completed to-dos across all projects...",
              file=sys.stderr)
        recordings = fetch_todos_by_assignee(person["id"], "completed", cli_timeout)
        print(f"  status=completed: {len(recordings)} to-do(s)", file=sys.stderr)
    else:
        # Stop paginating once the stream passes --since. Sorting by updated_at makes
        # this safe: completing a to-do bumps updated_at, so anything completed on or
        # after --since still has updated_at on or after it. Without --since there is
        # no window to stop at; with --full-scan the user has asked for everything.
        hard_stop = bool(since) and not args.full_scan
        sort = "updated" if hard_stop else "created"
        stop_before = since

        print(f"Streaming to-do listings account-wide (status: completed, "
              f"sorted {sort} desc)...", file=sys.stderr)
        if hard_stop:
            print(f"Will stop paginating at {since.isoformat()} "
                  f"(pass --full-scan to scan everything).", file=sys.stderr)
        elif since:
            print("--full-scan: paging through every to-do regardless of --since.",
                  file=sys.stderr)

        recordings = iter_todo_recordings(("completed",), sort, cli_timeout,
                                          stop_before=stop_before, hard_stop=hard_stop)

    if args.include_archived:
        # "archived" here covers a to-do whose todolist was individually
        # archived, even inside an otherwise-active project — see the
        # module docstring for why this needs a direct HTTP call instead
        # of any basecamp-cli command.
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
                # Completed to-dos in a wholesale-archived project — the
                # todolist-archival bug doesn't apply here, so the normal
                # CLI fetch works fine, scoped to just this one project.
                if args.by == "assignee":
                    recordings.extend(fetch_todos_by_assignee(
                        person["id"], "completed", cli_timeout, in_project=pid))
                else:
                    recordings.extend(fetch_completed_unfiltered(cli_timeout, pid))
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
        if not rec.get("completed"):
            continue
        row = shape_todo(rec)
        # --by assignee is server-side filtered (--assignee on every fetch
        # above) for every status except "archived", which always goes out
        # unfiltered (see fetch_todos_by_assignee()) and still needs the
        # client-side check. --by completer/either never had a server-side
        # filter at all, so the client-side check is the only thing doing
        # that job, regardless of status.
        needs_check = args.by != "assignee" or rec.get("status") == "archived"
        if needs_check and not matches_person(row, person["id"], args.by):
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
