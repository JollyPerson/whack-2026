
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
CHUNK_SAMPLES = CHUNK_BYTES // 2
RING_CHUNKS = 160


class AudioBuffer:
    """Thread-safe rolling audio buffer."""

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


def start_audio_capture(source, buffer):
    """Read 16-bit mono PCM from the PipeWire/PulseAudio source."""

    command = [
        "parec",
        "--device", source,
        "--format=s16le",
        "--rate=16000",
        "--channels=1",
        "--raw",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    def reader():
        while True:
            raw = process.stdout.read(CHUNK_BYTES)

            if not raw:
                break

            audio = np.frombuffer(raw, dtype=np.int16)
            audio = audio.astype(np.float32) / 32768.0
            buffer.add(audio)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    return process


def normalize_word(word):
    """Normalize a word for matching across overlapping transcriptions."""

    text = unicodedata.normalize("NFKC", word).lower()
    return re.sub(r"[^\w']", "", text)


def is_sentence_end(word):
    """Recognize common sentence-ending punctuation."""

    return bool(re.search(r'[.!?]["\')\]]*$', word.strip()))


class SentenceBuffer:
    """
    Holds uncommitted words from successive overlapping transcriptions.
    Emits a sentence only after its end is outside the holdback period.
    """

    def __init__(self):
        self.words = []
        self.committed_until = 0.0

    def add_words(self, new_words):
        for new in new_words:
            if new["end"] <= self.committed_until:
                continue

            normalized = normalize_word(new["word"])

            if not normalized and not is_sentence_end(new["word"]):
                continue

            # Match the same word in overlapping transcription windows.
            duplicate = None

            for old in self.words:
                if (
                    normalize_word(old["word"]) == normalized
                    and abs(old["start"] - new["start"]) <= 0.18
                ):
                    duplicate = old
                    break

            if duplicate is not None:
                # Prefer the most recent transcription's version.
                duplicate.update(new)
            else:
                self.words.append(new)

        self.words.sort(key=lambda item: item["start"])

    def extract_sentences(self, cutoff):
        """Return complete sentences whose final word is safe to commit."""

        emitted = []

        while True:
            boundary = None

            for i, word in enumerate(self.words):
                if is_sentence_end(word["word"]):
                    boundary = i
                    break

            if boundary is None:
                break

            sentence_words = self.words[:boundary + 1]
            sentence_end = sentence_words[-1]["end"]

            # Wait until the sentence is outside the holdback period.
            if sentence_end > cutoff:
                break

            emitted.append(self.make_sentence(sentence_words))

            self.committed_until = sentence_end
            self.words = self.words[boundary + 1:]

        return emitted

    @staticmethod
    def make_sentence(words):
        text = "".join(w["word"] for w in words).strip()

        return {
            "type": "sentence",
            "start": round(words[0]["start"], 3),
            "end": round(words[-1]["end"], 3),
            "text": text,
        }

    def flush(self):
        """Emit remaining complete sentences and any trailing fragment."""

        emitted = []

        while self.words:
            boundary = None

            for i, word in enumerate(self.words):
                if is_sentence_end(word["word"]):
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


def emit_sentence(sentence, output_file):
    """Write one final sentence to stdout and the JSONL output file."""

    line = json.dumps(sentence, ensure_ascii=False)

    print(line, flush=True)

    if output_file:
        output_file.write(line + "\n")
        output_file.flush()


def main():
    parser = argparse.ArgumentParser(
        description="Live microphone speech-to-text, sentence output only"
    )

    parser.add_argument("--source", required=True,
                        help="PipeWire/PulseAudio microphone source")
    parser.add_argument("--model", default="small.en")
    parser.add_argument("--window", type=float, default=6.0,
                        help="Audio window in seconds")
    parser.add_argument("--hop", type=float, default=1.0,
                        help="Seconds between transcription runs")
    parser.add_argument("--holdback", type=float, default=1.0,
                        help="Seconds to wait before committing words")
    parser.add_argument("--output", default="transcript.jsonl")

    args = parser.parse_args()

    print("Loading Whisper model...", file=sys.stderr)

    model = WhisperModel(
        args.model,
        device="cpu",
        compute_type="int8",
        cpu_threads=max(2, (os.cpu_count() or 4) - 1),
    )

    print("Starting audio capture...", file=sys.stderr)

    audio_buffer = AudioBuffer()
    sentence_buffer = SentenceBuffer()

    capture_process = start_audio_capture(args.source, audio_buffer)

    output_file = open(args.output, "a", encoding="utf-8")

    print("Listening. Press Ctrl+C to stop.", file=sys.stderr)

    next_run = time.monotonic()

    try:
        while True:
            audio, window_start, window_end = audio_buffer.snapshot(
                args.window
            )

            if audio is not None and len(audio) >= SAMPLE_RATE:
                segments, info = model.transcribe(
                    audio,
                    language="en",
                    beam_size=1,
                    word_timestamps=True,
                    condition_on_previous_text=False,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 400
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
                        })

                sentence_buffer.add_words(new_words)

                cutoff = window_end - args.holdback

                for sentence in sentence_buffer.extract_sentences(cutoff):
                    emit_sentence(sentence, output_file)

            next_run += args.hop
            time.sleep(max(0, next_run - time.monotonic()))

    except KeyboardInterrupt:
        print("\nStopping; flushing remaining text...", file=sys.stderr)

    finally:
        for sentence in sentence_buffer.flush():
            emit_sentence(sentence, output_file)

        capture_process.terminate()
        output_file.close()


if __name__ == "__main__":
    main()
