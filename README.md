# basecamp-fetch

A Python script that fetches all incomplete Basecamp todos assigned to you and outputs them as JSON (default) or an Obsidian-compatible markdown note. Run `python3 basecamp_fetch.py` for JSON output or `python3 basecamp_fetch.py --format markdown` to get a formatted note you can pipe directly into your vault. Requires Python 3.8+ and a single dependency (`requests>=2.28`), installed via `pip install -r requirements.txt`.

Credentials are read from a `.env` file in the same directory as the script. At minimum you need `BASECAMP_ACCESS_TOKEN` and `BASECAMP_ACCOUNT_ID`; adding `BASECAMP_REFRESH_TOKEN`, `BASECAMP_CLIENT_ID`, and `BASECAMP_CLIENT_SECRET` enables automatic token refresh — when a request returns 401 the script exchanges the refresh token for a new access token and writes it back to `.env`, so unattended cron or launchd runs stay working without intervention. See `SKILL.md` for the one-time OAuth2 setup walkthrough to obtain these values.

The script can be automated on Linux via `crontab` or on macOS via `launchd` (a sample plist is provided in `env-vars.plist.example`). Use absolute paths for both the Python interpreter and the script in any scheduled job, since cron and launchd run in a minimal environment without your shell's `PATH`. See `USAGE.md` for full cron/launchd examples and troubleshooting tips.
