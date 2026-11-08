#!/usr/bin/env bash
# Live speech-to-text with two microphones: ears/ears.py (local Whisper) -> ears/bridge.py -> the debate server.
#   MIC1=<source> MIC2=<source> scripts/ears-two-mics.sh
# MIC1 is Prop (speaker A) and MIC2 is Opp (speaker B). Run it without them to list the source names (also:
# pactl list short sources). A phone mic app such as MicYou shows up as the monitor of its virtual sink
# (MicYouVirtualSink.monitor); the monitors of the laptop's own outputs are left out, as they hear its speakers.
# Start the server first, with ears external: EARS=external ./run.sh live
# Needs uv (https://docs.astral.sh/uv/) and parec (PulseAudio/PipeWire). The first run creates ears/.venv from
# ears/uv.lock and downloads the Whisper model; after that it works offline.
# Optional: MODEL (default base.en), SERVER (default http://127.0.0.1:8000), WINDOW / HOP / HOLDBACK (default 6 / 1.5 /
# 2: the cleanest settings measured on an i5-10310U laptop; small.en is more accurate but ~4x slower per window).
set -euo pipefail
ROOT=$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)
SPEAKERS='^(alsa|bluez)_output[.].*[.]monitor$'
list_sources() {
    pactl list short sources 2>/dev/null | awk -v speakers="$SPEAKERS" '$2 !~ speakers {print "  " $2}' >&2 || true
}
if [ -z "${MIC1:-}" ] || [ -z "${MIC2:-}" ]; then
    echo "Set MIC1 and MIC2 to two different input sources (one microphone per debater):" >&2
    list_sources
    exit 2
fi
if [ "$MIC1" = "$MIC2" ]; then
    echo "MIC1 and MIC2 are both $MIC1: they must be two different sources, one per debater." >&2
    exit 2
fi
# Check the names against the sources there now (when pactl can list them): a typo or an unplugged mic would
# otherwise only show as ears retrying, deaf on that mic.
sources=$(pactl list short sources 2>/dev/null | awk '{print $2}' || true)
for mic in MIC1 MIC2; do
    name=${!mic}
    if [[ "$name" =~ $SPEAKERS ]]; then
        echo "$mic=$name is the monitor of the laptop's speakers: it hears what the laptop plays, not a debater." >&2
        exit 2
    fi
    if [ -n "$sources" ] && [[ "$name" != @* ]] && ! grep -qxF -- "$name" <<< "$sources"; then
        echo "$mic=$name is not a source right now (phone app not connected, mic unplugged, or a typo?). Sources:" >&2
        list_sources
        exit 2
    fi
    # The dashboard's voice and the dance music play to the default sink: if that is the phone mic's virtual sink,
    # its monitor hears them as that debater's speech.
    if [[ "$name" == *.monitor ]] && [ "$(pactl get-default-sink 2>/dev/null || true)" = "${name%.monitor}" ]; then
        echo "WARNING: the default sink is ${name%.monitor}, so $mic hears everything the laptop plays." >&2
        echo "  Play to the speakers instead: pactl set-default-sink <speakers> (pactl list short sinks)" >&2
    fi
done
command -v uv > /dev/null || { echo "uv is not installed: https://docs.astral.sh/uv/" >&2; exit 2; }
MODEL=${MODEL:-base.en}
SERVER=${SERVER:-http://127.0.0.1:8000}
mkdir -p "$ROOT/logs"
cd "$ROOT/ears"
uv run --frozen python ears.py --source mic1="$MIC1" --source mic2="$MIC2" --model "$MODEL" \
    --window "${WINDOW:-6}" --hop "${HOP:-1.5}" --holdback "${HOLDBACK:-2}" \
    --output "$ROOT/logs/ears-$(date +%Y%m%d-%H%M%S).jsonl" |
    uv run --frozen python bridge.py --url "$SERVER"
