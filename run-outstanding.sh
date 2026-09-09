#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

PERSON_ID="22567943"
FALLBACK_NAME="John David"

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

if [ -n "$PERSON_ID" ]; then
  USER_ARG="$PERSON_ID"
else
  USER_ARG="$FALLBACK_NAME"
  echo "note: PERSON_ID is unset — matching by name instead." >&2
  echo "      run '$PYTHON basecamp_outstanding.py --list-people echav' to get the id." >&2
fi

OUTPUT="self-outstanding-$(date +%Y-%m-%d).md"

"$PYTHON" basecamp_outstanding_cli.py \
  --user "$USER_ARG" \
  --since 2026-05-31 \
  --include-archived \
  --format markdown \
  -o "$OUTPUT" \
  -v
