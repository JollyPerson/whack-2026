
#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque

import numpy as np
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
CHUNK_BYTES = 8192
RING_CHUNKS = 160


class AudioBuffer:
    """Independent rolling audio buffer for each source."""

    def __init__(self):
        self.chunks = deque(maxlen=RING_CHUNKS)
        self.total_samples = 0
        self.lock = threading.Lock()

    def add(self, audio):
        with self.lock:
            self.chunks.append(audio)
            self.total_samples += len(audio)

    def snapshot(self, seconds):
        with self.lock:
            if not self.chunks:
                return None, 0.0, 0.0

            audio = np.concatenate(list(self.chunks))
            end_sample = self.total_samples

            wanted = int(seconds * SAMPLE_RATE)
            if len(audio) > wanted:
                audio = audio[-wanted:]

            start_sample = end_sample - len(audio)

            return (
                audio,
                start_sample / SAMPLE_RATE,
                end_sample / SAMPLE_RATE,
            )


class SentenceBuffer:
    """Independent uncommitted sentence buffer for each source."""

    def __init__(self):
        self.words = []
        self.committed_until = 0.0

    @staticmethod
    def normalize_word(word):
        text = unicodedata.normalize("NFKC", word).lower()
        return re.sub(r"[^\w']", "", text)

    # @staticmethod
    # def is_sentence_end(word):
    #     return bool(re.search(r'[.!?]["\')\]]*$', word.strip()))

    @staticmethod
    def is_sentence_end(text):
        """Return True only for a proper sentence-ending punctuation mark."""
        cleaned = text.rstrip()

        # An ellipsis is not a sentence boundary.
        if cleaned.endswith("...") or cleaned.endswith("…"):
            return False

        return bool(re.search(r"[.!?][\"')\]]*\s*$", cleaned))

    @staticmethod
    def remove_trailing_ellipsis(text):
        """Remove trailing three-dot or Unicode ellipsis."""
        return re.sub(r"(?:\.{3}|…)+\s*$", "", text).rstrip()

    def add_words(self, new_words):
        for new in new_words:
            if new["end"] <= self.committed_until:
                continue

            normalized = self.normalize_word(new["word"])

            if not normalized and not self.is_sentence_end(new["word"]):
                continue

            duplicate = None

            # Avoid duplicating words from overlapping audio windows.
            for old in self.words:
                if (
                    self.normalize_word(old["word"]) == normalized
                    and abs(old["start"] - new["start"]) <= 0.18
                ):
                    duplicate = old
                    break

            if duplicate is not None:
                duplicate.update(new)
            else:
                self.words.append(new)

        self.words.sort(key=lambda item: item["start"])



    @staticmethod
    def make_sentence(words):
        probabilities = [
            w["probability"]
            for w in words
            if w.get("probability") is not None
        ]

        confidence = (
            sum(probabilities) / len(probabilities)
            if probabilities else None
        )

        return {
            "type": "sentence",
            "start": round(words[0]["start"], 3),
            "end": round(words[-1]["end"], 3),
            "text": "".join(w["word"] for w in words).strip(),
            "confidence": round(confidence, 3) if confidence is not None else None,
        }

    # @staticmethod
    # def make_sentence(words):
    #     return {
    #         "type": "sentence",
    #         "start": round(words[0]["start"], 3),
    #         "end": round(words[-1]["end"], 3),
    #         "text": "".join(w["word"] for w in words).strip(),
    #     }

    def extract_sentences(self, cutoff):
        emitted = []

        while True:
            boundary = None

            for i, word in enumerate(self.words):
                text = word["word"].rstrip()

                # Ignore ellipses as sentence boundaries and remove them.
                if text.endswith("...") or text.endswith("…"):
                    word["word"] = re.sub(r"(?:\.{3}|…)+$", "", text).rstrip()
                    continue

                if self.is_sentence_end(text):
                    boundary = i
                    break

            if boundary is None:
                break

            sentence_words = self.words[:boundary + 1]
            sentence_end = sentence_words[-1]["end"]

            if sentence_end > cutoff:
                break

            emitted.append(self.make_sentence(sentence_words))

            self.committed_until = sentence_end
            self.words = self.words[boundary + 1:]

        return emitted


    def flush(self):
        """Emit remaining sentences and any trailing fragment on shutdown."""

        emitted = []

        while self.words:
            boundary = None

            for i, word in enumerate(self.words):
                if self.is_sentence_end(word["word"]):
                    boundary = i
                    break

            if boundary is None:
                emitted.append(self.make_sentence(self.words))
                self.words = []
            else:
                sentence_words = self.words[:boundary + 1]
                emitted.append(self.make_sentence(sentence_words))
                self.words = self.words[boundary + 1:]

        return emitted


def start_audio_capture(source_name, device, buffer):
    """Start one independent parec process and audio reader."""

    command = [
        "parec",
        "--device", device,
        "--format=s16le",
        "--rate=16000",
        "--channels=1",
        "--raw",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        bufsize=0,
    )

    def reader():
        while True:
            raw = process.stdout.read(CHUNK_BYTES)

            if not raw:
                break

            # Ensure the PCM data contains complete 16-bit samples.
            raw = raw[:len(raw) - (len(raw) % 2)]
            if not raw:
                continue

            audio = np.frombuffer(raw, dtype=np.int16)
            audio = audio.astype(np.float32) / 32768.0
            buffer.add(audio)

        print(
            f"Audio capture ended for {source_name}",
            file=sys.stderr,
        )

    thread = threading.Thread(
        target=reader,
        daemon=True,
        name=f"capture-{source_name}",
    )
    thread.start()

    return process


def emit_sentence(sentence, source_name, output_file):
    """Emit one final sentence, tagged with its source."""

    event = {
        "type": "sentence",
        "source": source_name,
        "start": sentence["start"],
        "end": sentence["end"],
        "text": sentence["text"],
    }

    line = json.dumps(event, ensure_ascii=False)

    print(line, flush=True)

    if output_file:
        output_file.write(line + "\n")
        output_file.flush()



def transcribe_source(model, source_name, state, args, output_file):
    """Transcribe the latest window for one source."""

    audio, window_start, window_end = state["audio"].snapshot(
        args.window
    )

    if audio is None or len(audio) < SAMPLE_RATE:
        return

    segments, info = model.transcribe(
        audio,
        language="en",
        beam_size=3,
        word_timestamps=True,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 1200
        },
    )

    new_words = []

    for segment in segments:
        if not segment.words:
            continue

        for word in segment.words:
            new_words.append({
                "word": word.word,
                "start": window_start + word.start,
                "end": window_start + word.end,
                "probability": word.probability,
            })

    state["sentences"].add_words(new_words)

    cutoff = window_end - args.holdback

    for sentence in state["sentences"].extract_sentences(cutoff):
        emit_sentence(sentence, source_name, output_file)


# def transcribe_source(model, source_name, state, args, output_file):
#     """Transcribe the latest window for one source."""

#     audio, window_start, window_end = state["audio"].snapshot(
#         args.window
#     )

#     if audio is None or len(audio) < SAMPLE_RATE:
#         return

#     segments, info = model.transcribe(
#         audio,
#         language="en",
#         beam_size=3,
#         word_timestamps=True,
#         condition_on_previous_text=False,
#         vad_filter=True,
#         vad_parameters={
#             "min_silence_duration_ms": 1200
#         },
#     )

    new_words = []

    for segment in segments:
        if not segment.words:
            continue

        for word in segment.words:
            new_words.append({
                "word": word.word,
                "start": window_start + word.start,
                "end": window_start + word.end,
            })

    state["sentences"].add_words(new_words)

    cutoff = window_end - args.holdback

    for sentence in state["sentences"].extract_sentences(cutoff):
        emit_sentence(sentence, source_name, output_file)


def main():
    parser = argparse.ArgumentParser(
        description="Two-source live speech-to-text"
    )

    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="NAME=DEVICE",
        help="Audio source; specify twice, e.g. mic1=device1",
    )

    parser.add_argument("--model", default="small.en")
    parser.add_argument("--window", type=float, default=6.0)
    parser.add_argument("--hop", type=float, default=1.0)
    parser.add_argument("--holdback", type=float, default=1.0)
    parser.add_argument("--output", default="transcript.jsonl")

    args = parser.parse_args()

    if len(args.source) != 2:
        parser.error("Specify exactly two --source arguments.")

    sources = {}

    for item in args.source:
        if "=" not in item:
            parser.error(
                f"Invalid source '{item}'. Use NAME=DEVICE."
            )

        name, device = item.split("=", 1)

        if not name or not device:
            parser.error(
                f"Invalid source '{item}'. Both name and device are required."
            )

        if name in sources:
            parser.error(f"Duplicate source name: {name}")

        sources[name] = device


    print("Loading Whisper model...", file=sys.stderr)

    # One model is shared; inference is performed sequentially
    # to avoid simultaneous CPU inference competing for resources.
    model = WhisperModel(
        args.model,
        device="cpu",
        compute_type="int8",
        cpu_threads=max(2, (os.cpu_count() or 4) - 1),
    )

    states = {}
    processes = {}

    output_file = open(args.output, "a", encoding="utf-8")

    try:
        for name, device in sources.items():
            print(
                f"Starting audio source {name}: {device}",
                file=sys.stderr,
            )

            audio_buffer = AudioBuffer()

            states[name] = {
                "audio": audio_buffer,
                "sentences": SentenceBuffer(),
                "next_run": time.monotonic(),
            }

            processes[name] = start_audio_capture(
                name,
                device,
                audio_buffer,
            )

        print(
            "Listening to both sources. Press Ctrl+C to stop.",
            file=sys.stderr,
        )

        while True:
            for name, state in states.items():
                now = time.monotonic()

                if now < state["next_run"]:
                    continue

                transcribe_source(
                    model,
                    name,
                    state,
                    args,
                    output_file,
                )

                # Schedule from the completion time to avoid
                # accumulating a backlog if inference is slow.
                state["next_run"] = time.monotonic() + args.hop

            time.sleep(0.02)

    except KeyboardInterrupt:
        print(
            "\nStopping; flushing remaining sentences...",
            file=sys.stderr,
        )

    finally:
        for name, state in states.items():
            for sentence in state["sentences"].flush():
                emit_sentence(sentence, name, output_file)

        for process in processes.values():
            process.terminate()

        for process in processes.values():
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

        output_file.close()


if __name__ == "__main__":
    main()
