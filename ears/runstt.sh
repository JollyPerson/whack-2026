python ears.py \
    --source MicYouVirtualSink.monitor \
    --model small.en \
    --window 4 \
    --hop 0.5 \
    --holdback 1.4 \
    --output transcript.jsonl \
    | tee events.jsonl