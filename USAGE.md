---
type: reference
date: 2026-03-31
tags:
  - type/reference
  - topic/automation
  - source/basecamp
---

# Basecamp Fetch — Usage & Cron Setup

Fetches all incomplete Basecamp todos assigned to you and outputs them as JSON or an Obsidian-ready markdown note.

---

## Prerequisites

- Python 3.8+

Create a virtual environment and install dependencies:

```bash
cd basecamp-skill
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Activate the venv (optional, makes subsequent commands shorter):

```bash
source .venv/bin/activate
```

---

## Configuration

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

The script automatically refreshes an expired access token on a 401 and writes the new token back to `.env`, so cron runs stay unattended.

---

## Usage

### JSON output (default)

```bash
.venv/bin/python3 basecamp_fetch.py
# or, if venv is activated:
python3 basecamp_fetch.py
```

Prints a JSON array to stdout:

```json
[
  {
    "project": "Acme Corp",
    "list": "Sprint 12",
    "task": "Write release notes",
    "due": "2026-04-01",
    "url": "https://3.basecamp.com/..."
  }
]
```

### Markdown output

```bash
.venv/bin/python3 basecamp_fetch.py --format markdown
```

Prints an Obsidian-compatible markdown note:

```markdown
---
type: reference
date: 2026-03-31
tags:
  - type/reference
  - source/basecamp
---

# Basecamp Tasks

## Acme Corp

### Sprint 12
- [ ] Write release notes — due 2026-04-01 · [view](https://3.basecamp.com/...)
```

Pipe directly into your vault:

```bash
.venv/bin/python3 basecamp_fetch.py --format markdown \
  > ~/vault/00-Inbox/basecamp-tasks.md
```

---

## Setting Up a Cron Job

### Linux (crontab)

Open your crontab:

```bash
crontab -e
```

Add a line. Examples:

```cron
# Run every weekday at 8am, write markdown note to vault inbox
0 8 * * 1-5 /path/to/basecamp-skill/.venv/bin/python3 /path/to/basecamp-skill/basecamp_fetch.py --format markdown > /path/to/vault/00-Inbox/basecamp-tasks.md 2>> /var/log/basecamp-fetch.log

# Run every weekday at 8am, append JSON to a log file
0 8 * * 1-5 /path/to/basecamp-skill/.venv/bin/python3 /path/to/basecamp-skill/basecamp_fetch.py >> /var/log/basecamp-todos.json 2>> /var/log/basecamp-fetch.log
```

**Tips:**
- Use absolute paths for both the Python interpreter and the script — cron runs in a minimal environment with no `PATH`.
- Find your Python path with `which python3`.
- Stderr (token refresh messages, errors) is redirected to a log file separately from the output.

---

### macOS (launchctl)

On macOS, `launchd` is preferred over cron. A plist file defines the job and is loaded into `launchctl`.

#### 1. Create the plist

Save the following to `~/Library/LaunchAgents/com.yourname.basecamp-fetch.plist`:

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

#### 2. Load the job

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.basecamp-fetch.plist
```

#### 3. Test it immediately

```bash
launchctl start com.yourname.basecamp-fetch
```

Check the output file and error log:

```bash
cat ~/vault/00-Inbox/basecamp-tasks.md
cat ~/Library/Logs/basecamp-fetch.log
```

#### 4. Unload / disable

```bash
launchctl unload ~/Library/LaunchAgents/com.yourname.basecamp-fetch.plist
```

**Tips:**
- `~/Library/LaunchAgents/` jobs run as your user and have access to your home directory — no sudo needed.
- launchd does **not** inherit your shell's `PATH` or environment. Use full absolute paths everywhere.
- If you manage credentials via `launchctl setenv` instead of `.env`, see `env-vars.plist.example` in this repo for a helper plist that sets env vars at login.
- To check job status: `launchctl list | grep basecamp`

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `BASECAMP_ACCESS_TOKEN and BASECAMP_ACCOUNT_ID must be set` | Check `.env` path — must be in the same directory as the script |
| `Token refresh failed (401)` | Refresh token has expired; re-run the OAuth flow in `SKILL.md` |
| Empty output file on macOS | Check `StandardErrorPath` log; likely a bad Python path or missing `.env` |
| Cron runs but file not updated | Ensure the output path in the crontab is absolute and writable |
