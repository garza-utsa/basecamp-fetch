#!/usr/bin/env bash
#
# Completed Basecamp tasks for Pamela Dyer, AY 2025-2026.
# Writes dyer-2025-2026.md next to this script.
#
# Must be run from a terminal with network access to 3.basecampapi.com.
#
# ---------------------------------------------------------------------------
# WHY A PERSON ID AND NOT AN EMAIL
#
# Basecamp only returns `email_address` to account admins and owners. For any
# other token the field is null for everyone except yourself, so an email
# lookup has nothing to match against. The numeric person id always works.
#
# Find it once:
#
#   ./basecamp_completed.py --list-people dyer
#
# which prints lines like:
#
#     43217890  Pamela Dyer                    (email hidden)
#     ^^^^^^^^
#     this is PERSON_ID
#
# Then set it below. Leave it empty to fall back to a name lookup.
# ---------------------------------------------------------------------------

set -euo pipefail

cd "$(dirname "$0")"

PERSON_ID="39664829" # <-- paste Pamela's numeric id here, e.g. PERSON_ID="43217890"
FALLBACK_NAME="Lallo Vigil"

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

if [ -n "$PERSON_ID" ]; then
  USER_ARG="$PERSON_ID"
else
  USER_ARG="$FALLBACK_NAME"
  echo "note: PERSON_ID is unset — matching by name instead." >&2
  echo "      run '$PYTHON basecamp_completed.py --list-people dyer' to get the id." >&2
fi

"$PYTHON" basecamp_completed.py \
  --user "$USER_ARG" \
  --since 2026-05-01 --until 2026-08-31 \
  --include-archived \
  --format markdown \
  -o vigil-summer-2026.md \
  -v
