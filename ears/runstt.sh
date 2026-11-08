#!/usr/bin/env bash
# Live speech-to-text for both mics, from wherever it's called. Pipe it into bridge.py to send the sentences on.
cd "$(dirname "$0")"
uv run python ears.py \
    --source mic1=MicYouVirtualSink.monitor \
    --source mic2=alsa_input.usb-Samson_Technologies_Samson_Meteor_Mic-00.analog-stereo \
    --model small.en \
    --window 4 \
    --hop 0.5 \
    --holdback 1 \
    --output transcript.jsonl \
    | tee events.jsonl
