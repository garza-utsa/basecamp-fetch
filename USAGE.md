---
type: reference
date: 2026-09-09
tags:
  - type/reference
  - topic/automation
  - source/basecamp
---

# Basecamp Reporting Scripts — Usage

There are two families of scripts in this repo:

- **CLI-backed (recommended):** `basecamp_fetch_cli.py`, `basecamp_outstanding_cli.py`,
  `basecamp_completed_cli.py`. Authentication is entirely owned by the
  [`basecamp` CLI](https://github.com/basecamp/basecamp-cli) — no `.env` file, no OAuth
  plumbing to manage yourself.
- **REST-based (legacy):** `basecamp_fetch.py`, `basecamp_outstanding.py`,
  `basecamp_completed.py`. Manage their own OAuth tokens in a `.env` file. See
  [Legacy: REST-based scripts](#legacy-rest-based-scripts-env) below.

Use the CLI-backed scripts unless you have a specific reason not to (e.g. the `basecamp`
CLI isn't installed on the machine that needs to run this).

---

## Prerequisites (CLI-backed scripts)

- Python 3.8+ with `requests` installed:

  ```bash
  cd basecamp-skill
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```

- The `basecamp` CLI installed ([github.com/basecamp/basecamp-cli](https://github.com/basecamp/basecamp-cli))
  and authenticated **once**:

  ```bash
  basecamp auth login
  ```

  The CLI stores and auto-refreshes credentials itself (system keychain, falling back to
  `~/.config/basecamp/credentials.json` if the keychain is unavailable). None of the
  CLI-backed scripts read or write that file directly.

---

## The three CLI-backed scripts

### `basecamp_fetch_cli.py` — your own outstanding to-dos

```bash
./basecamp_fetch_cli.py --format markdown -o my-tasks.md
```

Flags: `--format {json,markdown}`, `--output/-o` (repeatable), `--verbose/-v`,
`--timeout` (default 300s per CLI call, `0` = none).

### `basecamp_outstanding_cli.py` — a teammate's outstanding to-dos

```bash
# Find their numeric id first (Basecamp hides email_address from non-admin tokens,
# so name/email matching often can't resolve — the id always works):
./basecamp_outstanding_cli.py --list-people jane

./basecamp_outstanding_cli.py --user <PERSON_ID> --include-archived \
  --format markdown -o outstanding-jane.md
```

Flags: `--user/-u`, `--list-people [FILTER]`, `--project/-p`, `--since`/`--until`
(due-date window), `--as-of`, `--overdue-only`, `--format {json,markdown,csv}`,
`--output/-o`, `--include-archived`, `--lookback-months` (default 6 — excludes
still-open to-dos *created* more than N months ago; `0` disables), `--verbose/-v`,
`--timeout` (default 900s per CLI call).

### `basecamp_completed_cli.py` — a teammate's completed to-dos

```bash
./basecamp_completed_cli.py --list-people jane

./basecamp_completed_cli.py --user <PERSON_ID> \
  --since 2025-09-01 --until 2025-12-31 \
  --include-archived \
  --format markdown -o completed-jane-fall-2025.md
```

Flags: `--user/-u`, `--list-people [FILTER]`, `--project/-p`, `--since`/`--until`
(completion-date window), `--format {json,markdown,csv}`, `--output/-o`,
`--by {assignee,completer,either}` (default `assignee`), `--full-scan`,
`--include-archived`, `--verbose/-v`, `--timeout` (default 900s per CLI call).

`--by assignee` (the default) is exact and fast: it asks the CLI for exactly this
person's completed to-dos, server-side filtered. `--by completer`/`--by either` answer a
different question — "what did they personally check off, regardless of who it was
assigned to" — and require a slower, unfiltered account-wide scan, bounded by `--since`
unless `--full-scan` is passed.

---

## Generating a full completed-work report for a date range in the past

This is the recipe for "what did this person finish between date X and date Y" —
performance-review prep, quarterly/annual overviews, etc.

```bash
# 1. Get their numeric id (once — save it in a wrapper script, see run-dyer.sh
#    for the pattern):
./basecamp_completed_cli.py --list-people dyer

# 2. Run the report. --include-archived is not optional for a "full" report:
./basecamp_completed_cli.py \
  --user 39524204 \
  --since 2025-09-01 --until 2025-12-31 \
  --include-archived \
  --format markdown \
  -o completed-dyer-fall-2025.md \
  -v
```

**`--include-archived` is required for completeness, not just "nice to have."** As
projects and old todolists get cleaned up over time, completed to-dos end up inside
*individually archived todolists* — independent of whether their project is archived.
Basecamp's own CLI has no way to list those (`--status completed`, `--status archived`,
`recordings todos`, even the raw `api get` passthrough all miss them — this is a
confirmed CLI limitation, not something these scripts choose to skip). Without
`--include-archived`, a report can silently under-count real completed work — on one
account, the same report window that should have returned 23 completed to-dos returned
only 6 without it.

To close that gap, `--include-archived` makes one direct HTTP call to the raw
`/projects/recordings.json?status=archived` endpoint — the same endpoint the legacy REST
script uses — walking real pagination that the CLI itself doesn't expose. It's
authenticated via a token read from `basecamp auth token` (never printed or logged), so
you still don't need a `.env` file. This means:

- **It's slower.** Expect one full account-wide crawl of every archived-status to-do,
  plus one additional pass per archived project. `-v` will show progress
  (`status=archived: N to-do(s) account-wide`, `Scanned N to-do(s)`, etc.) so a long run
  doesn't look stuck.
- **It's still worth running every time** you need a report that covers more than the
  last few weeks — the older the window, the more likely relevant todolists have since
  been archived.

If you only need a quick, recent-window check (not a report you're relying on for
completeness), you can omit `--include-archived` and get the fast path only.

---

## Scheduling (cron / launchd)

The CLI-backed scripts need `basecamp` on `PATH` and already authenticated (`basecamp
auth login` run once, interactively, ahead of time) — cron/launchd jobs run in a minimal
environment, so use absolute paths and confirm `basecamp auth status` succeeds under the
same user/session the job runs as.

### Linux (crontab)

```bash
crontab -e
```

```cron
# Run every weekday at 8am, write markdown note to vault inbox
0 8 * * 1-5 /path/to/basecamp-skill/.venv/bin/python3 /path/to/basecamp-skill/basecamp_fetch_cli.py --format markdown -o /path/to/vault/00-Inbox/basecamp-tasks.md 2>> /var/log/basecamp-fetch.log
```

### macOS (launchctl)

Same pattern as the legacy plist below (see
[Legacy: REST-based scripts](#legacy-rest-based-scripts-env)) — just point
`ProgramArguments` at `basecamp_fetch_cli.py` instead of `basecamp_fetch.py`, and drop the
`--format`/`--output` split in favor of `-o <path>` (the CLI-backed script writes to file
itself rather than relying on `StandardOutPath`).

**Tips:**
- `launchd` does **not** inherit your shell's `PATH` — use `/usr/bin/env basecamp` or the
  full path from `which basecamp` if `ProgramArguments` needs to invoke `basecamp`
  directly for any reason (the wrapper scripts here always call it via `python3`, which
  itself shells out to `basecamp`, so the venv's Python needs `PATH` to include wherever
  `basecamp` lives).
- To check job status: `launchctl list | grep basecamp`

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `basecamp CLI error: Not authenticated...` | Run `basecamp auth login` (interactively, once) |
| `'basecamp' CLI not found on PATH` | Install from [github.com/basecamp/basecamp-cli](https://github.com/basecamp/basecamp-cli) |
| A completed-work report looks short / under-counts | Missing `--include-archived` — see [above](#generating-a-full-completed-work-report-for-a-date-range-in-the-past) |
| `--include-archived` run is slow / many `RUN` lines in `-v` output | Expected — it's a full account-wide crawl of archived-status to-dos plus one pass per archived project, not a bug |
| `No person matched '...'` | Basecamp hides `email_address` from non-admin tokens; use `--list-people <substring>` to find the numeric id instead |
| `'...' matches several people` | Re-run with the numeric id shown in the error |

---

## Legacy: REST-based scripts (`.env`)

`basecamp_fetch.py`, `basecamp_outstanding.py`, and `basecamp_completed.py` predate the
`basecamp` CLI integration and manage their own OAuth tokens directly against the
Basecamp REST API.

### Configuration

Create a `.env` file in the same directory as the script (`basecamp-skill/.env`):

```env
BASECAMP_ACCESS_TOKEN="your_access_token"
BASECAMP_ACCOUNT_ID="123456789"

# Required for automatic token refresh (recommended for cron):
BASECAMP_REFRESH_TOKEN="your_refresh_token"
BASECAMP_CLIENT_ID="your_client_id"
BASECAMP_CLIENT_SECRET="your_client_secret"
```

> See `SKILL.md` for the full OAuth2 setup walkthrough to obtain these values.

The script automatically refreshes an expired access token on a 401 and writes the new
token back to `.env`, so cron runs stay unattended.

### Usage

```bash
# JSON to stdout
.venv/bin/python3 basecamp_fetch.py

# Markdown, piped into a vault
.venv/bin/python3 basecamp_fetch.py --format markdown > ~/vault/00-Inbox/basecamp-tasks.md
```

### Cron (Linux)

```cron
0 8 * * 1-5 /path/to/basecamp-skill/.venv/bin/python3 /path/to/basecamp-skill/basecamp_fetch.py --format markdown > /path/to/vault/00-Inbox/basecamp-tasks.md 2>> /var/log/basecamp-fetch.log
```

**Tips:**
- Use absolute paths for both the Python interpreter and the script — cron runs in a
  minimal environment with no `PATH`.
- Find your Python path with `which python3`.
- Stderr (token refresh messages, errors) is redirected to a log file separately from
  the output.

### launchd (macOS)

Save to `~/Library/LaunchAgents/com.yourname.basecamp-fetch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>

  <key>Label</key>
  <string>com.yourname.basecamp-fetch</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/yourname/path/to/basecamp-skill/.venv/bin/python3</string>
    <string>/Users/yourname/path/to/basecamp-skill/basecamp_fetch.py</string>
    <string>--format</string>
    <string>markdown</string>
  </array>

  <!-- Weekdays at 08:00 -->
  <key>StartCalendarInterval</key>
  <array>
    <dict>
      <key>Weekday</key><integer>1</integer>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
    <dict>
      <key>Weekday</key><integer>2</integer>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
    <dict>
      <key>Weekday</key><integer>3</integer>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
    <dict>
      <key>Weekday</key><integer>4</integer>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
    <dict>
      <key>Weekday</key><integer>5</integer>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
  </array>

  <!-- Redirect output to your vault inbox -->
  <key>StandardOutPath</key>
  <string>/Users/yourname/vault/00-Inbox/basecamp-tasks.md</string>

  <key>StandardErrorPath</key>
  <string>/Users/yourname/Library/Logs/basecamp-fetch.log</string>

  <!-- Run immediately on load (useful for first-time testing) -->
  <key>RunAtLoad</key>
  <false/>

</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.basecamp-fetch.plist
launchctl start com.yourname.basecamp-fetch   # test immediately
launchctl unload ~/Library/LaunchAgents/com.yourname.basecamp-fetch.plist  # disable
```

**Tips:**
- `~/Library/LaunchAgents/` jobs run as your user and have access to your home
  directory — no sudo needed.
- launchd does **not** inherit your shell's `PATH` or environment. Use full absolute
  paths everywhere.
- If you manage credentials via `launchctl setenv` instead of `.env`, see
  `env-vars.plist.example` in this repo for a helper plist that sets env vars at login.
- To check job status: `launchctl list | grep basecamp`

### Troubleshooting (legacy scripts)

| Symptom | Fix |
|---|---|
| `BASECAMP_ACCESS_TOKEN and BASECAMP_ACCOUNT_ID must be set` | Check `.env` path — must be in the same directory as the script |
| `Token refresh failed (401)` | Refresh token has expired; re-run the OAuth flow in `SKILL.md` |
| Empty output file on macOS | Check `StandardErrorPath` log; likely a bad Python path or missing `.env` |
| Cron runs but file not updated | Ensure the output path in the crontab is absolute and writable |
