#!/usr/bin/env bash
# Fast gate before every push: unit + smoke tests (< 30 s).
# Install the hook once:  git config core.hooksPath .githooks
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  ./run.sh --help > /dev/null
fi
exec .venv/bin/python -m pytest -q -x "$@"
