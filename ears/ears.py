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
# A window's first words may be cut off mid-word: older words win there.
HEAD_MARGIN = 0.4
# The same word this soon after the previous one is one word heard twice.
DUPLICATE_GAP = 0.3
# Words with no sentence end after them, this long before the cutoff, are a
# sentence anyway: the speaker trailed off or Whisper left out the period.
TAIL_IDLE = 2.0
# Ends in a period but rarely ends a sentence: "U.S.", "e.g.", "Dr.".
ABBREVIATION = re.compile(r"(?:[a-z]\.){2,}|(?:mr|mrs|ms|dr|prof|st|vs|jr|sr)\.", re.IGNORECASE)
# Seconds before each parec restart, the last one repeating; a capture that ran PAREC_HEALTHY seconds starts over.
PAREC_BACKOFF = (0.5, 1.0, 2.0)
PAREC_HEALTHY = 10.0
# Levels: exact digital silence this long is a deaf source, not a quiet room (a live mic always has some noise).
SILENCE_WARN = 15.0
SILENT_PEAK = 2 / 32768
QUIET_PEAK = 10 ** (-30 / 20)
# This many full-scale samples in one read is clipping, reported at most every CLIP_WARN_EVERY seconds.
CLIP_PEAK = 32767 / 32768
CLIP_SAMPLES = 3
CLIP_WARN_EVERY = 10.0
# Seconds of audio between two peak-level lines per source.
LEVEL_EVERY = 15.0


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
        self.last_committed = None

    @staticmethod
    def normalize_word(word):
        text = unicodedata.normalize("NFKC", word).lower()
        return re.sub(r"[^\w']", "", text)

    @staticmethod
    def is_sentence_end(text):
        """Return True only for a proper sentence-ending punctuation mark."""
        cleaned = text.rstrip()

        # An ellipsis is not a sentence boundary.
        if cleaned.endswith("...") or cleaned.endswith("…"):
            return False

        # Nor is an abbreviation: "The U.S. imported grain." is one claim.
        if cleaned and ABBREVIATION.fullmatch(cleaned.split()[-1].lstrip("\"'([")):
            return False

        return bool(re.search(r"[.!?][\"')\]]*\s*$", cleaned))

    @staticmethod
    def remove_trailing_ellipsis(text):
        """Remove trailing three-dot or Unicode ellipsis."""
        return re.sub(r"(?:\.{3}|…)+\s*$", "", text).rstrip()

    def add_words(self, new_words, window_start=None):
        """Merge the words of one window into the uncommitted ones.

        The newest window wins: it heard more audio around each word, so it
        replaces older words from window_start + HEAD_MARGIN on. Before that,
        where its first word may be cut off mid-word, the older words stay.
        Without window_start, the first new word's start stands in for it.
        """
        fresh = []

        for new in new_words:
            # Already emitted. The midpoint, not the end, so a re-heard last
            # word that jitter pushed just past the boundary stays out.
            if (new["start"] + new["end"]) / 2 <= self.committed_until:
                continue

            # Punctuation on its own: faster-whisper splits "%" off "57%" (and
            # "%," or "%."), and only merges some marks back. Put it back on
            # its word; with no word before it in this window, drop it.
            if not self.normalize_word(new["word"]):
                if fresh:
                    fresh[-1]["word"] += new["word"]
                    fresh[-1]["end"] = max(fresh[-1]["end"], new["end"])
                continue

            fresh.append(dict(new))

        if not fresh:
            return

        fresh.sort(key=lambda item: item["start"])

        if window_start is None:
            window_start = fresh[0]["start"]

        kept = [old for old in self.words if old["start"] < window_start + HEAD_MARGIN]
        splice = max([old["end"] for old in kept], default=self.committed_until)
        fresh = [new for new in fresh if (new["start"] + new["end"]) / 2 > splice]

        # The same word again right where the kept (or emitted) words end is
        # one word heard twice with jittered timestamps.
        previous = kept[-1] if kept else self.last_committed
        if (
            fresh and previous
            and self.normalize_word(fresh[0]["word"]) == self.normalize_word(previous["word"])
            and fresh[0]["start"] - previous["end"] < DUPLICATE_GAP
        ):
            fresh.pop(0)

        self.words = sorted(kept + fresh, key=lambda item: item["start"])


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

    def _commit(self, count):
        """Take the first count words out as one sentence."""
        sentence_words, self.words = self.words[:count], self.words[count:]
        self.committed_until = sentence_words[-1]["end"]
        self.last_committed = sentence_words[-1]
        return self.make_sentence(sentence_words)

    def extract_sentences(self, cutoff):
        """Emit the sentences that end by cutoff, and the words after the
        last boundary once they end TAIL_IDLE before it."""
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

            if self.words[boundary]["end"] > cutoff:
                break

            emitted.append(self._commit(boundary + 1))

        # Trailing off, cut off, or Whisper left out the period: once the
        # speaker has been quiet a while it is a sentence of its own, not the
        # start of their next sentence minutes later.
        if self.words and self.words[-1]["end"] <= cutoff - TAIL_IDLE:
            emitted.append(self._commit(len(self.words)))

        return [sentence for sentence in emitted if sentence["text"]]

    def flush(self):
        """Emit remaining sentences and any trailing fragment on shutdown."""
        return self.extract_sentences(float("inf"))


def say(message):
    """One line for the operator, on stderr: what the terminal running ears shows."""
    print(message, file=sys.stderr, flush=True)


def dbfs(peak):
    return f"{20 * np.log10(peak):.0f} dBFS" if peak >= SILENT_PEAK else "no signal at all"


class LevelWatch:
    """Tells the operator, from one source's audio alone, what parec cannot: that the mic is deaf (pure digital
    silence: a monitor source such as MicYouVirtualSink.monitor keeps sending zeros after the phone drops off the
    Wi-Fi), that it clips, and its peak level now and then. Times are in seconds of audio. add() returns the lines."""

    def __init__(self, source_name, device):
        self.name, self.device = source_name, device
        self.silent_for = 0.0
        self.silent_warned = False
        self.since_clip_warning = None
        self.since_level = 0.0
        self.peak = 0.0

    def add(self, audio):
        said = []
        if not len(audio):
            return said
        seconds = len(audio) / SAMPLE_RATE
        magnitude = np.abs(audio)
        peak = float(magnitude.max())

        if peak < SILENT_PEAK:
            self.silent_for += seconds
            if self.silent_for >= SILENCE_WARN and not self.silent_warned:
                self.silent_warned = True
                said.append(
                    f"!! ears: {self.name} ({self.device}) has sent pure digital silence for "
                    f"{self.silent_for:.0f} s: is the phone app disconnected or muted, or the volume at 0? "
                    f"{self.name} hears nothing until then"
                )
        else:
            if self.silent_warned:
                said.append(f"ears: sound again from {self.name} ({self.device})")
            self.silent_for, self.silent_warned = 0.0, False

        if self.since_clip_warning is not None:
            self.since_clip_warning += seconds
        if (
            np.count_nonzero(magnitude >= CLIP_PEAK) >= CLIP_SAMPLES
            and (self.since_clip_warning is None or self.since_clip_warning >= CLIP_WARN_EVERY)
        ):
            self.since_clip_warning = 0.0
            said.append(
                f"!! ears: {self.name} ({self.device}) is clipping: turn its gain down "
                f"(pactl set-source-volume {self.device} 70%, or the phone app's or the mic's own gain)"
            )

        self.peak = max(self.peak, peak)
        self.since_level += seconds
        if self.since_level >= LEVEL_EVERY:
            hint = ""
            if SILENT_PEAK <= self.peak < QUIET_PEAK:
                hint = " (quiet: if someone spoke into it, move it closer or raise its gain)"
            said.append(
                f"ears: {self.name} peak level over the last {self.since_level:.0f} s: {dbfs(self.peak)}{hint}"
            )
            self.peak, self.since_level = 0.0, 0.0

        return said


class Capture:
    """One source's parec, started again with backoff whenever it ends, so a mic that drops out (phone app
    restarted, Wi-Fi drop, USB replug) comes back by itself instead of staying deaf. parec is told never to fall
    back to another source: PipeWire would otherwise move it to the default one (the speakers' monitor, for a
    monitor source) and this mic would transcribe the wrong audio. So a missing source makes parec exit, and the
    restarts wait for it to come back. terminate(), wait() and kill() are those of the parec running now."""

    def __init__(self, source_name, device, buffer):
        self.name, self.device, self.buffer = source_name, device, buffer
        self.command = [
            "parec",
            "--device", device,
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--raw",
            "--property=node.dont-fallback=true",
        ]
        self.levels = LevelWatch(source_name, device)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.process = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"capture-{source_name}")
        self.thread.start()

    def _start(self):
        with self.lock:
            if self.stopping.is_set():
                return None
            try:
                # Its own session: Ctrl+C reaches ears alone, which then stops parec itself (not taken for a
                # dropped mic). If ears dies, parec's next write to the closed pipe ends it.
                self.process = subprocess.Popen(
                    self.command,
                    stdout=subprocess.PIPE,
                    stderr=sys.stderr,
                    bufsize=0,
                    start_new_session=True,
                )
            except OSError as error:
                self.process = None
                say(f"!! ears: cannot start parec for {self.name}: {error}")
            return self.process

    def _read(self, process, restarted):
        odd = b""
        while True:
            raw = process.stdout.read(CHUNK_BYTES)

            if not raw:
                return

            # Keep any odd byte for the next read: dropping it would misalign every later sample.
            raw = odd + raw
            whole = len(raw) - len(raw) % 2
            raw, odd = raw[:whole], raw[whole:]
            if not raw:
                continue

            if restarted:
                say(f"ears: capturing {self.device} again: hearing {self.name} again")
                restarted = False

            audio = np.frombuffer(raw, dtype=np.int16)
            audio = audio.astype(np.float32) / 32768.0
            self.buffer.add(audio)
            for line in self.levels.add(audio):
                say(line)

    def _run(self):
        failures = 0
        while not self.stopping.is_set():
            process = self._start()
            started = time.monotonic()
            code = None
            if process is not None:
                self._read(process, restarted=failures > 0)
                code = process.wait()

            if self.stopping.is_set():
                break

            if time.monotonic() - started >= PAREC_HEALTHY:
                failures = 0
            delay = PAREC_BACKOFF[min(failures, len(PAREC_BACKOFF) - 1)]
            failures += 1
            say(
                f"!! ears: audio capture for {self.name} ({self.device}) ended (parec exit {code}): "
                f"{self.name} is deaf; retrying in {delay:g} s"
            )
            self.stopping.wait(delay)

        say(f"Audio capture ended for {self.name}")

    def terminate(self):
        with self.lock:
            self.stopping.set()
            process = self.process
        if process is not None and process.poll() is None:
            process.terminate()

    def wait(self, timeout=None):
        self.thread.join(timeout)
        process = self.process
        return process.wait(timeout) if process is not None else None

    def kill(self):
        process = self.process
        if process is not None:
            process.kill()


def start_audio_capture(source_name, device, buffer):
    """Start one independent parec process and audio reader, restarted whenever it ends."""

    return Capture(source_name, device, buffer)


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

    # The file first: on Ctrl+C the reader of stdout may be gone already.
    if output_file:
        output_file.write(line + "\n")
        output_file.flush()

    try:
        print(line, flush=True)
    except OSError:
        stdout_gone()


def stdout_gone():
    """The reader of stdout (tee, bridge.py) has exited: point stdout at
    /dev/null, so later sentences and the flush at exit stop raising. The
    transcript file still gets every sentence."""

    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        return

    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)

    print("stdout closed; sentences go to the transcript file only", file=sys.stderr)



def transcribe_source(model, source_name, state, args, output_file):
    """Transcribe the latest window for one source."""

    audio, window_start, window_end = state["audio"].snapshot(
        args.window
    )

    if audio is None or len(audio) < SAMPLE_RATE:
        return

    # Nothing new since the last run: parec delivers in bursts, and nothing
    # at all once a mic is gone. The same audio decodes to the same words.
    if window_end == state.get("transcribed_until"):
        return

    previous = state.get("transcribed_until")
    state["transcribed_until"] = window_end

    # Windows overlap while ears keeps up; a gap between them is speech nobody will ever see.
    if previous is not None and window_start > previous:
        say(
            f"!! ears: {source_name} is falling behind: {window_start - previous:.1f} s of audio was never "
            f"transcribed. Use a smaller MODEL (base.en, tiny.en) or a longer WINDOW"
        )

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

    state["sentences"].add_words(new_words, window_start)

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
        try:
            for name, state in states.items():
                for sentence in state["sentences"].flush():
                    emit_sentence(sentence, name, output_file)
        finally:
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
