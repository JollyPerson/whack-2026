#!/usr/bin/env bash
# ./run.sh [live|drive|mock|dev-a|dev-b|dev-c|dev-d|replay] [--file X] [--speed N] [--only Utterance Pause]
#          [--all-sessions] [--max-gap S]
# Per-role override: EARS|BRAIN|EVIDENCE|FACE = real|mock|off|external|pkg.module:function
# SKIP_PIP=1 never runs pip (offline venue).
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
fi
# Reinstall whenever ANY requirements file changes (each role edits its own requirements/<role>.txt).
SUM=$(cat requirements.txt requirements/*.txt 2>/dev/null | cksum)
if [ -z "${SKIP_PIP:-}" ] && [ "$(cat .venv/.deps 2>/dev/null)" != "$SUM" ]; then
  if .venv/bin/pip install -q --retries 2 --timeout 10 -r requirements.txt; then
    echo "$SUM" > .venv/.deps
  elif .venv/bin/python -c "import fastapi, uvicorn, pydantic" 2>/dev/null; then
    echo "WARN: pip install failed (offline?) - starting with the existing .venv; a role whose new package" \
         "is missing shows a red dot. Rerun ./run.sh when online." >&2
  else
    echo "ERROR: pip install failed and .venv can't start the server - see the pip error above." >&2
    exit 1
  fi
fi
# .env is sourced so it still wins over the shell's exports, but never fatally: a line bash can't run (a value with
# spaces, a `$`) only prints an error here, and app/config.py loads the same file with python-dotenv, which fills
# in whatever bash skipped.
if [ -f .env ]; then
  set -a +eu
  . ./.env
  set +a -eu
fi
mkdir -p logs cache
exec .venv/bin/python -m app.main "$@"
