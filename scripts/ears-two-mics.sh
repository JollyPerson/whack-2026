#!/usr/bin/env bash
# Live speech-to-text with two microphones: ears/ears.py (local Whisper) -> ears/bridge.py -> the debate server.
#   MIC1=<source> MIC2=<source> scripts/ears-two-mics.sh
# MIC1 is Prop (speaker A) and MIC2 is Opp (speaker B). List the source names with: pactl list short sources
# Start the server first, with ears external: EARS=external ./run.sh live
# Needs uv (https://docs.astral.sh/uv/) and parec (PulseAudio/PipeWire). The first run creates ears/.venv from
# ears/uv.lock and downloads the Whisper model; after that it works offline.
# Optional: MODEL (default base.en), SERVER (default http://127.0.0.1:8000), WINDOW / HOP / HOLDBACK (default 6 / 1.5 /
# 2: the cleanest settings measured on an i5-10310U laptop; small.en is more accurate but ~4x slower per window).
set -euo pipefail
ROOT=$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)
if [ -z "${MIC1:-}" ] || [ -z "${MIC2:-}" ]; then
    echo "Set MIC1 and MIC2 to two different input sources (one microphone per debater):" >&2
    pactl list short sources 2>/dev/null | awk '$2 !~ /\.monitor$/ {print "  " $2}' >&2
    exit 2
fi
command -v uv > /dev/null || { echo "uv is not installed: https://docs.astral.sh/uv/" >&2; exit 2; }
MODEL=${MODEL:-base.en}
SERVER=${SERVER:-http://127.0.0.1:8000}
mkdir -p "$ROOT/logs"
cd "$ROOT/ears"
uv run --frozen python ears.py --source mic1="$MIC1" --source mic2="$MIC2" --model "$MODEL" \
    --window "${WINDOW:-6}" --hop "${HOP:-1.5}" --holdback "${HOLDBACK:-2}" \
    --output "$ROOT/logs/ears-$(date +%Y%m%d-%H%M%S).jsonl" |
    uv run --frozen python bridge.py --url "$SERVER"
