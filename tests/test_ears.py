"""ears/ears.py (Ears' live speech-to-text): sentence stitching across overlapping windows, transcription and shutdown.
faster_whisper isn't installed here, so it's stubbed while the module loads; no audio, no model."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent

# Only the stub comes and goes: patch.dict would also drop every module ears.py imports (numpy can't load twice).
_had_whisper = "faster_whisper" in sys.modules
_real_whisper = sys.modules.get("faster_whisper")
sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=object)
try:
    _spec = importlib.util.spec_from_file_location("ears_stt", ROOT / "ears" / "ears.py")
    ears = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ears)
finally:
    if _had_whisper:
        sys.modules["faster_whisper"] = _real_whisper
    else:
        del sys.modules["faster_whisper"]


def w(text, start, end, probability=0.9):
    return {"word": text, "start": start, "end": end, "probability": probability}


def texts(sb, cutoff):
    return [s["text"] for s in sb.extract_sentences(cutoff)]


# ----------------------------------------------------------------------------- transcribe_source
class FakeModel:
    """Returns a list of segments (BatchedInferencePipeline style, not a one-shot generator)."""

    def __init__(self, words):
        self.words, self.calls = words, 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        seg = types.SimpleNamespace(words=[types.SimpleNamespace(word=t, start=s, end=e, probability=.9)
                                           for t, s, e in self.words])
        return [seg], None


def source_state(seconds=2.0):
    audio = ears.AudioBuffer()
    audio.add(np.zeros(int(seconds * ears.SAMPLE_RATE), dtype=np.float32))
    return {"audio": audio, "sentences": ears.SentenceBuffer()}


ARGS = types.SimpleNamespace(window=4.0, holdback=0.5)


def test_transcribe_source_adds_each_word_once(monkeypatch):
    state = source_state()
    added = []
    real_add = state["sentences"].add_words
    monkeypatch.setattr(state["sentences"], "add_words", lambda words, *a, **kw: (added.append(words),
                                                                                 real_add(words, *a, **kw)))
    out = io.StringIO()
    with mock.patch("sys.stdout", io.StringIO()):
        ears.transcribe_source(FakeModel([(" Hello", 0.1, 0.4), (" there.", 0.4, 0.8)]), "mic1", state, ARGS, out)
    assert len(added) == 1 and len(added[0]) == 2
    assert [json.loads(line)["text"] for line in out.getvalue().splitlines()] == ["Hello there."]


def test_transcribe_source_tells_the_buffer_where_the_window_starts(monkeypatch):
    state = source_state(seconds=6.0)                        # a 4 s window over 6 s of audio starts at 2 s
    calls = []
    monkeypatch.setattr(state["sentences"], "add_words", lambda words, *a, **kw: calls.append((words, a, kw)))
    ears.transcribe_source(FakeModel([(" Hi", 0.5, 0.8)]), "mic1", state, ARGS, None)
    [(words, a, kw)] = calls
    assert words[0]["start"] == pytest.approx(2.5)
    assert (a or (kw.get("window_start"),))[0] == pytest.approx(2.0)


def test_audio_already_transcribed_is_not_transcribed_again():
    # parec delivers in bursts (and not at all once a mic is gone): the same audio decodes to the same words.
    state, model = source_state(), FakeModel([(" Hi", 0.5, 0.8)])
    for _ in range(3):
        ears.transcribe_source(model, "mic1", state, ARGS, None)
    assert model.calls == 1
    state["audio"].add(np.zeros(ears.SAMPLE_RATE // 2, dtype=np.float32))
    ears.transcribe_source(model, "mic1", state, ARGS, None)
    assert model.calls == 2


# ----------------------------------------------------------------------------- stitching overlapping windows
@pytest.mark.parametrize("second_window_start", [7.5, 9.9])   # the word well inside the window, or in its head
def test_a_word_seen_again_with_jittered_timestamps_is_one_word(second_window_start):
    sb = ears.SentenceBuffer()
    sb.add_words([w(" He", 10.0, 10.2), w(" spoke.", 10.3, 10.8)], window_start=7.0)
    sb.add_words([w(" He", 10.25, 10.4), w(" spoke.", 10.35, 10.8)], window_start=second_window_start)
    assert texts(sb, 20) == ["He spoke."]


def test_a_respelled_word_replaces_the_older_spelling():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" It's", 3.0, 3.2), w(" OK", 3.2, 3.5)], window_start=0.0)
    sb.add_words([w(" It's", 3.0, 3.2), w(" okay.", 3.25, 3.6)], window_start=0.5)
    assert texts(sb, 20) == ["It's okay."]


def test_the_newest_window_replaces_a_tail_cut_off_by_the_older_one():
    # The first window ended mid-sentence: Whisper put a period on "29" and the next window re-heard it later.
    sb = ears.SentenceBuffer()
    sb.add_words([w(" we", 0.0, 0.2), w(" spend", 0.2, 0.5), w(" 29.", 0.5, 0.9)], window_start=0.0)
    assert texts(sb, 0.9 - 1.0) == []                        # held back
    sb.add_words([w(" we", 0.0, 0.2), w(" spend", 0.2, 0.5), w(" 29", 0.7, 1.0), w(" percent", 1.0, 1.3),
                  w(" more.", 1.3, 1.6)], window_start=0.0)
    assert texts(sb, 20) == ["we spend 29 percent more."]


def test_words_cut_off_at_the_head_of_a_window_keep_their_older_version():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" important", 9.8, 10.3), w(" point.", 10.4, 10.9)], window_start=7.0)
    sb.add_words([w(" tant", 10.0, 10.3), w(" point.", 10.4, 10.9)], window_start=10.0)
    assert texts(sb, 20) == ["important point."]


def test_a_window_that_hears_nothing_keeps_the_words_already_heard():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" Yes.", 1.0, 1.4)], window_start=0.0)
    sb.add_words([], window_start=0.5)
    assert texts(sb, 20) == ["Yes."]


@pytest.mark.parametrize("copy", [w(" you", 9.75, 10.05), w(" you", 9.95, 10.25), w(" you.", 9.8, 10.1)])
def test_the_last_word_of_an_emitted_sentence_does_not_leak_into_the_next(copy):
    sb = ears.SentenceBuffer()
    sb.add_words([w(" Thank", 9.5, 9.7), w(" you.", 9.7, 10.0)], window_start=7.0)
    assert texts(sb, 20) == ["Thank you."]
    sb.add_words([copy, w(" Next", 10.5, 10.7), w(" point.", 10.7, 11.0)], window_start=7.5)
    assert texts(sb, 20) == ["Next point."]


def test_a_repeated_word_well_apart_is_still_two_words():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" No,", 1.0, 1.3), w(" no.", 2.0, 2.3)], window_start=0.0)
    sb.add_words([w(" No,", 1.0, 1.3), w(" no.", 2.0, 2.3)], window_start=1.2)
    assert texts(sb, 20) == ["No, no."]


@pytest.mark.parametrize("words, text", [
    ([w(" 29", .5, .9), w("%", .9, 1.1), w(" of", 1.1, 1.2), w(" people.", 1.2, 1.6)], "29% of people."),
    ([w(" rose", 0, .4), w(" 57", .5, .9), w("%,", .9, 1.1), w(" which", 1.1, 1.4), w(" is", 1.4, 1.5),
      w(" a", 1.5, 1.6), w(" lot.", 1.6, 2.0)], "rose 57%, which is a lot."),
    ([w(" about", 0, .4), w(" 2", .5, .8), w("%.", .8, 1.0)], "about 2%."),
])
def test_punctuation_whisper_splits_off_stays_on_its_word(words, text):
    # faster-whisper makes "%" a word of its own and doesn't merge it back like "." or ",".
    sb = ears.SentenceBuffer()
    sb.add_words(words, window_start=0.0)
    assert texts(sb, 20) == [text]


def test_punctuation_split_off_a_word_already_emitted_is_dropped():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" It", 0, .2), w(" works.", .2, .6)], window_start=0.0)
    assert texts(sb, 20) == ["It works."]
    sb.add_words([w(" works.", .2, .6), w("%", .6, .62), w(" Then", 1.0, 1.2), w(" more.", 1.2, 1.5)], window_start=0.0)
    assert texts(sb, 20) == ["Then more."]


@pytest.mark.parametrize("words, text", [
    ([w(" The", 0, .2), w(" U.S.", .2, .6), w(" imported", .6, 1.0), w(" grain.", 1.0, 1.4)],
     "The U.S. imported grain."),
    ([w(" Dr.", 0, .3), w(" Smith", .3, .6), w(" agrees.", .6, 1.0)], "Dr. Smith agrees."),
    ([w(" Cities,", 0, .4), w(" e.g.", .4, .8), w(" London.", .8, 1.2)], "Cities, e.g. London."),
])
def test_abbreviations_do_not_end_a_sentence(words, text):
    sb = ears.SentenceBuffer()
    sb.add_words(words, window_start=0.0)
    assert texts(sb, 20) == [text]


@pytest.mark.parametrize("word, end", [("29.", True), ("U.S.?", True), ("Plan A.", True), ("so...", False),
                                       ("so…", False), ("U.S.", False), ("Mr.", False), ("vs.", False)])
def test_sentence_end(word, end):
    assert ears.SentenceBuffer.is_sentence_end(" " + word) is end


def test_an_unfinished_sentence_is_emitted_once_the_speaker_goes_quiet():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" and", 10.0, 10.2), w(" so...", 10.2, 10.6)], window_start=7.0)
    assert texts(sb, 11.0) == []                             # maybe just a breath
    [tail] = sb.extract_sentences(13.0)
    assert (tail["text"], tail["start"], tail["end"]) == ("and so", 10.0, 10.6)
    # ...and not glued onto the same speaker's next sentence a minute later.
    sb.add_words([w(" Second", 75.0, 75.4), w(" point.", 75.4, 75.9)], window_start=72.0)
    [nxt] = sb.extract_sentences(100)
    assert (nxt["text"], nxt["start"]) == ("Second point.", 75.0)


def test_a_sentence_ending_in_an_abbreviation_still_comes_out():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" Grain", 1.0, 1.3), w(" from", 1.3, 1.5), w(" the", 1.5, 1.6), w(" U.S.", 1.6, 2.0)],
                 window_start=0.0)
    assert texts(sb, 2.5) == []
    assert texts(sb, 4.5) == ["Grain from the U.S."]


def test_flush_emits_the_finished_sentences_and_the_fragment_after_them():
    sb = ears.SentenceBuffer()
    sb.add_words([w(" One.", 0, .5), w(" Two", 1.0, 1.2), w(" more.", 1.2, 1.5), w(" Three", 2.0, 2.2)],
                 window_start=0.0)
    assert texts(sb, 0.0) == []
    assert [s["text"] for s in sb.flush()] == ["One.", "Two more.", "Three"]
    assert sb.words == []


# ----------------------------------------------------------------------------- output and shutdown
class ClosedPipe:
    """stdout once tee / bridge.py has exited (Ctrl+C reaches the whole pipeline)."""

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_stdout_does_not_lose_the_sentence_from_the_transcript(monkeypatch):
    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    out = io.StringIO()
    ears.emit_sentence({"start": 1.0, "end": 2.0, "text": "Last words."}, "mic1", out)
    assert json.loads(out.getvalue()) == {"type": "sentence", "source": "mic1", "start": 1.0, "end": 2.0,
                                          "text": "Last words."}


class FakeParec:
    def __init__(self):
        self.terminated = self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True

    def kill(self):
        pass


def test_ctrl_c_with_stdout_gone_still_flushes_every_source_and_stops_capture(monkeypatch, tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    procs = []

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["ears.py", "--source", "mic1=dev1", "--source", "mic2=dev2",
                                      "--output", str(transcript)])
    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    monkeypatch.setattr(ears, "WhisperModel", lambda *a, **kw: object())
    monkeypatch.setattr(ears, "start_audio_capture", lambda *a: procs.append(FakeParec()) or procs[-1])
    monkeypatch.setattr(ears, "transcribe_source", lambda *a: None)
    monkeypatch.setattr(ears, "time", types.SimpleNamespace(monotonic=lambda: 0.0, sleep=interrupt))
    monkeypatch.setattr(ears.SentenceBuffer, "flush",
                        lambda self: [{"start": 1.0, "end": 2.0, "text": "Unfinished"}])
    ears.main()
    assert [json.loads(line)["source"] for line in transcript.read_text().splitlines()] == ["mic1", "mic2"]
    assert len(procs) == 2 and all(p.terminated and p.waited for p in procs)


# ----------------------------------------------------------------------------- runstt.sh
def test_runstt_runs_from_any_directory(tmp_path):
    # A copy, so tee writes its events.jsonl next to the copy and never over the recorded ears/events.jsonl.
    import os
    import shutil
    import subprocess
    here = tmp_path / "ears"
    here.mkdir()
    shutil.copy2(ROOT / "ears" / "runstt.sh", here / "runstt.sh")
    (tmp_path / "bin").mkdir()
    uv = tmp_path / "bin" / "uv"
    uv.write_text('#!/usr/bin/env bash\nprintf \'{"type": "sentence", "cwd": "%s", "args": "%s"}\\n\' "$PWD" "$*"\n')
    uv.chmod(0o755)
    r = subprocess.run(["./ears/runstt.sh"], cwd=tmp_path, capture_output=True, text=True, timeout=20,
                       env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    line = json.loads(r.stdout)
    assert line["cwd"] == str(here)
    assert line["args"].startswith("run python ears.py --source mic1=")
    assert (here / "events.jsonl").read_text() == r.stdout


# ----------------------------------------------------------------------------- capture: parec dies, comes back
class ScriptedParec:
    """A parec that hands out `chunks` from stdout, then ends with `rc` (or blocks until terminated)."""

    def __init__(self, chunks=(), rc=1, block=False):
        import threading
        self.chunks, self.rc, self.block = list(chunks), rc, block
        self.ended = threading.Event()
        self.stdout = self
        self.returncode = None

    def read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        if self.block:
            self.ended.wait(5)
        return b""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.block:
            self.ended.wait(timeout)
        self.returncode = self.rc
        return self.rc

    def terminate(self):
        self.ended.set()

    kill = terminate


class FakePopen:
    """subprocess.Popen standing in for parec: each start takes the next ScriptedParec (or exception) in turn."""

    def __init__(self, *runs):
        self.runs, self.commands, self.started = list(runs), [], []

    def __call__(self, command, **kw):
        self.commands.append(command)
        run = self.runs.pop(0) if self.runs else ScriptedParec(block=True)
        if isinstance(run, BaseException):
            raise run
        self.started.append(run)
        return run


def pcm(*samples):
    return np.array(samples, dtype=np.int16).tobytes()


def wait_until(cond, timeout=3.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


@pytest.fixture
def quick_restarts(monkeypatch):
    monkeypatch.setattr(ears, "PAREC_BACKOFF", (0.01,))


def test_parec_never_falls_back_to_another_source(monkeypatch, quick_restarts):
    # PipeWire moves a stream whose source vanished (phone app restarted, USB mic unplugged) to the default: the
    # speakers' monitor for a monitor source like MicYouVirtualSink.monitor, else the laptop mic. Probed on this
    # laptop: without node.dont-fallback, parec keeps running there, so mic1 would transcribe the laptop's speakers.
    popen = FakePopen(ScriptedParec(block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    capture = ears.start_audio_capture("mic1", "MicYouVirtualSink.monitor", ears.AudioBuffer())
    try:
        assert wait_until(lambda: popen.commands)
        assert "--property=node.dont-fallback=true" in popen.commands[0]
        assert popen.commands[0][popen.commands[0].index("--device") + 1] == "MicYouVirtualSink.monitor"
    finally:
        capture.terminate()
        capture.wait(2)


def test_a_capture_that_ends_is_restarted_and_says_so_loudly(monkeypatch, quick_restarts, capsys):
    popen = FakePopen(ScriptedParec([pcm(1, 2)], rc=1), ScriptedParec(rc=1), ScriptedParec([pcm(3, 4)], block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    buffer = ears.AudioBuffer()
    capture = ears.start_audio_capture("mic2", "usb-mic", buffer)
    try:
        assert wait_until(lambda: buffer.total_samples == 4)
        assert len(popen.commands) == 3
        audio, _, _ = buffer.snapshot(1)
        assert list(np.round(audio * 32768)) == [1, 2, 3, 4]
    finally:
        capture.terminate()
        capture.wait(2)
    err = capsys.readouterr().err
    assert "mic2" in err and "usb-mic" in err and "deaf" in err and "retrying" in err
    assert "hearing mic2 again" in err


def test_ctrl_c_stops_the_restarts(monkeypatch, quick_restarts):
    # The device is missing: every parec exits at once. Once stopped, no parec may start again (or outlive ears).
    monkeypatch.setattr(ears, "PAREC_BACKOFF", (0.05,))
    popen = FakePopen(*[ScriptedParec(rc=1) for _ in range(1000)])
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    capture = ears.start_audio_capture("mic1", "gone", ears.AudioBuffer())
    assert wait_until(lambda: len(popen.commands) >= 2)
    capture.terminate()
    capture.wait(2)
    started = len(popen.commands)
    import time
    time.sleep(0.2)
    assert len(popen.commands) == started
    assert not capture.thread.is_alive()


def test_terminate_stops_the_running_parec(monkeypatch, quick_restarts):
    run = ScriptedParec(block=True)
    monkeypatch.setattr(ears.subprocess, "Popen", FakePopen(run))
    capture = ears.start_audio_capture("mic1", "dev", ears.AudioBuffer())
    assert wait_until(lambda: capture.process is run)
    capture.terminate()
    assert capture.wait(2) == 1
    assert run.ended.is_set() and not capture.thread.is_alive()


def test_parec_missing_is_reported_and_retried_not_a_silent_dead_thread(monkeypatch, quick_restarts, capsys):
    popen = FakePopen(FileNotFoundError(2, "No such file or directory", "parec"), ScriptedParec([pcm(5)], block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    buffer = ears.AudioBuffer()
    capture = ears.start_audio_capture("mic1", "dev", buffer)
    try:
        assert wait_until(lambda: buffer.total_samples == 1)
    finally:
        capture.terminate()
        capture.wait(2)
    assert "parec" in capsys.readouterr().err


def test_an_odd_sized_read_keeps_the_samples_aligned(monkeypatch, quick_restarts):
    # Dropping the odd byte shifted every later sample by one byte: loud noise for the rest of the run.
    data = pcm(1000, -2000, 3000, -4000)
    monkeypatch.setattr(ears.subprocess, "Popen", FakePopen(ScriptedParec([data[:3], data[3:]], block=True)))
    buffer = ears.AudioBuffer()
    capture = ears.start_audio_capture("mic1", "dev", buffer)
    try:
        assert wait_until(lambda: buffer.total_samples == 4)
        audio, _, _ = buffer.snapshot(1)
        assert list(np.round(audio * 32768)) == [1000, -2000, 3000, -4000]
    finally:
        capture.terminate()
        capture.wait(2)


# ----------------------------------------------------------------------------- levels: silence, clipping
def chunk(value, seconds):
    return np.full(int(seconds * ears.SAMPLE_RATE), value / 32768, dtype=np.float32)


def test_a_source_sending_pure_silence_is_reported_once_and_its_return_too():
    # A null sink's monitor (MicYouVirtualSink.monitor) keeps sending zeros after the phone drops off Wi-Fi:
    # parec never exits, so only the level shows the mic is deaf.
    watch = ears.LevelWatch("mic1", "MicYouVirtualSink.monitor")
    said = []
    for _ in range(int(ears.SILENCE_WARN) - 1):
        said += watch.add(chunk(0, 1.0))
    assert not [m for m in said if "silence" in m]
    for _ in range(5):
        said += watch.add(chunk(0, 1.0))
    assert len([m for m in said if "silence" in m]) == 1
    said += watch.add(chunk(800, 0.5))
    assert len([m for m in said if "sound again" in m]) == 1


def test_a_quiet_but_live_mic_is_not_called_silent():
    watch = ears.LevelWatch("mic2", "usb")
    noise = (np.random.default_rng(1).standard_normal(ears.SAMPLE_RATE) * 20 / 32768).astype(np.float32)
    assert not [m for _ in range(40) for m in watch.add(noise) if "silence" in m]


def test_clipping_is_reported_with_a_gain_hint_at_most_every_few_seconds():
    watch = ears.LevelWatch("mic2", "usb")
    loud = chunk(32767, 0.25)
    said = [m for _ in range(8) for m in watch.add(loud)]            # 2 s of clipping
    assert len([m for m in said if "clipping" in m]) == 1
    assert "gain" in said[0]


def test_levels_are_reported_now_and_then():
    watch = ears.LevelWatch("mic1", "dev")
    said = [m for _ in range(int(ears.LEVEL_EVERY) * 2) for m in watch.add(chunk(3277, 1.0))]   # -20 dBFS
    levels = [m for m in said if "dBFS" in m]
    assert len(levels) == 2 and "-20 dBFS" in levels[0]

