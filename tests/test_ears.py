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
    monkeypatch.setattr(ears, "listed_sources", lambda: None)      # pactl cannot say: parec starts anyway


class Listings:
    """listed_sources() standing in for pactl: each call takes the next set of names in turn, the last repeating."""

    def __init__(self, *listings):
        self.listings, self.calls = list(listings), 0

    def __call__(self):
        self.calls += 1
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]


def test_parec_never_moves_to_another_source(monkeypatch, quick_restarts):
    # PipeWire moves a stream whose source vanished (phone app restarted, USB mic unplugged) to the default: the
    # speakers' monitor for a monitor source like MicYouVirtualSink.monitor, else the laptop mic. Probed on this
    # laptop: node.dont-reconnect makes parec exit then ("Entity killed"). node.dont-fallback must not be used: with
    # it parec cannot record the default source or the default sink's monitor at all, and PipeWire often makes a
    # freshly plugged USB mic the default source.
    popen = FakePopen(ScriptedParec(block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    capture = ears.start_audio_capture("mic1", "MicYouVirtualSink.monitor", ears.AudioBuffer())
    try:
        assert wait_until(lambda: popen.commands)
        assert "--property=node.dont-reconnect=true" in popen.commands[0]
        assert not any("dont-fallback" in arg for arg in popen.commands[0])
        assert popen.commands[0][popen.commands[0].index("--device") + 1] == "MicYouVirtualSink.monitor"
    finally:
        capture.terminate()
        capture.wait(2)


def test_a_source_that_is_not_there_is_never_recorded_until_it_is(monkeypatch, quick_restarts, capsys):
    # parec started on a missing source records the default one instead (probed: 64000 bytes in 3 s from
    # no_such_source.monitor). So ears looks the source up first and waits for it, deaf, as if parec had ended.
    listings = Listings({"28063", "laptop-mic"}, {"laptop-mic"}, {"laptop-mic", "usb-mic"})
    monkeypatch.setattr(ears, "listed_sources", listings)
    popen = FakePopen(ScriptedParec([pcm(7)], block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    buffer = ears.AudioBuffer()
    capture = ears.start_audio_capture("mic2", "usb-mic", buffer)
    try:
        assert wait_until(lambda: buffer.total_samples == 1)
        assert listings.calls == 3 and len(popen.commands) == 1
    finally:
        capture.terminate()
        capture.wait(2)
    err = capsys.readouterr().err
    assert err.count("usb-mic is not a current source") == 2 and "mic2 is deaf; retrying" in err
    assert "pactl list short sources" in err and "hearing mic2 again" in err


@pytest.mark.parametrize("device, listing", [("@DEFAULT_SOURCE@", set()), ("28063", {"28063", "laptop-mic"}),
                                             ("usb-mic", None)])
def test_a_default_alias_an_index_or_no_pactl_starts_parec_at_once(monkeypatch, quick_restarts, device, listing):
    monkeypatch.setattr(ears, "listed_sources", Listings(listing))
    popen = FakePopen(ScriptedParec(block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    capture = ears.start_audio_capture("mic1", device, ears.AudioBuffer())
    try:
        assert wait_until(lambda: popen.commands)
    finally:
        capture.terminate()
        capture.wait(2)


def test_listed_sources_reads_names_and_indexes_from_pactl(monkeypatch):
    listing = ("28061\talsa_output.x.HiFi__Headphones__sink.monitor\tPipeWire\ts32le 2ch 48000Hz\tRUNNING\n"
               "28070\tMicYouVirtualSink.monitor\tPipeWire\ts16le 1ch 16000Hz\tIDLE\n")
    ran = []

    def run(command, **kw):
        ran.append(command)
        return types.SimpleNamespace(stdout=listing, returncode=0)

    monkeypatch.setattr(ears.subprocess, "run", run)
    assert ears.listed_sources() == {"28061", "alsa_output.x.HiFi__Headphones__sink.monitor", "28070",
                                     "MicYouVirtualSink.monitor"}
    assert ran == [["pactl", "list", "short", "sources"]]

    def broken(command, **kw):
        raise FileNotFoundError(2, "No such file or directory", "pactl")

    monkeypatch.setattr(ears.subprocess, "run", broken)
    assert ears.listed_sources() is None


def test_a_mic_deaf_for_good_says_so_now_and_then_not_every_retry(monkeypatch, quick_restarts, capsys):
    # A missing source retried every 2 s for the whole debate buried the level and falling-behind lines.
    monkeypatch.setattr(ears, "DEAF_LOUD", 3)
    popen = FakePopen(*[ScriptedParec(rc=1) for _ in range(30)], ScriptedParec([pcm(9)], block=True))
    monkeypatch.setattr(ears.subprocess, "Popen", popen)
    buffer = ears.AudioBuffer()
    capture = ears.start_audio_capture("mic1", "gone", buffer)
    try:
        assert wait_until(lambda: buffer.total_samples == 1)
    finally:
        capture.terminate()
        capture.wait(2)
    err = capsys.readouterr().err
    assert err.count("mic1 is deaf") == 3
    assert f"every {ears.DEAF_EVERY:g} s" in err
    assert "hearing mic1 again" in err


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


def test_a_mic_that_came_back_and_drops_again_says_so_at_once(monkeypatch, quick_restarts, capsys):
    # Probed live: after 'hearing mic2 again', unloading its sink a few seconds later printed no deaf line at all.
    monkeypatch.setattr(ears, "DEAF_LOUD", 3)
    runs = [ScriptedParec(rc=1) for _ in range(5)] + [ScriptedParec([pcm(1)], rc=1), ScriptedParec(rc=1),
                                                      ScriptedParec([pcm(2)], block=True)]
    monkeypatch.setattr(ears.subprocess, "Popen", FakePopen(*runs))
    buffer = ears.AudioBuffer()
    capture = ears.start_audio_capture("mic2", "usb-mic", buffer)
    try:
        assert wait_until(lambda: buffer.total_samples == 2)
    finally:
        capture.terminate()
        capture.wait(2)
    err = capsys.readouterr().err
    assert err.count("mic2 is deaf") == 3 + 2
    assert err.count("hearing mic2 again") == 2


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


# ----------------------------------------------------------------------------- falling behind
def test_falling_behind_is_reported(capsys):
    state = source_state(seconds=10.0)
    state["transcribed_until"] = 2.0                         # the last window ended at 2 s; this one starts at 6 s
    ears.transcribe_source(FakeModel([]), "mic1", state, ARGS, None)
    assert "behind" in capsys.readouterr().err
    state["audio"].add(np.zeros(ears.SAMPLE_RATE, dtype=np.float32))
    ears.transcribe_source(FakeModel([]), "mic1", state, ARGS, None)    # overlapping windows: all heard
    assert "behind" not in capsys.readouterr().err


# ----------------------------------------------------------------------------- scripts/ears-two-mics.sh
SOURCES = """\
28058\talsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink.monitor\tPipeWire\ts32le 2ch 48000Hz\tRUNNING
28063\talsa_input.pci-0000_00_1f.3.HiFi__Mic1__source\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED
31000\tMicYouVirtualSink.monitor\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING
31001\talsa_input.usb-Samson_Technologies_Samson_Meteor_Mic-00.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tIDLE
"""
USB = "alsa_input.usb-Samson_Technologies_Samson_Meteor_Mic-00.analog-stereo"


def launcher_tree(tmp_path, ears_body='echo \'{"type": "sentence"}\'', default_sink="alsa_output.pci.HiFi__Speaker__sink"):
    """A copy of the launcher with fake uv and pactl: uv's ears prints a sentence (or runs ears_body), its bridge
    saves what it reads to bridge.in."""
    import shutil
    (tmp_path / "scripts").mkdir()
    (tmp_path / "ears").mkdir()
    shutil.copy2(ROOT / "scripts" / "ears-two-mics.sh", tmp_path / "scripts")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "uv").write_text(f'#!/usr/bin/env bash\ncase "$*" in\n  *ears.py*) {ears_body} ;;\n'
                             f'  *bridge.py*) cat > "{tmp_path}/bridge.in" ;;\nesac\n')
    (bin_ / "pactl").write_text('#!/usr/bin/env bash\ncase "$*" in\n'
                                f'  "list short sources") printf %s "$FAKE_SOURCES" ;;\n'
                                f'  get-default-sink) echo "{default_sink}" ;;\nesac\n')
    for tool in ("uv", "pactl"):
        (bin_ / tool).chmod(0o755)
    return tmp_path


def run_launcher(tree, timeout=20, **env):
    import os
    import subprocess
    return subprocess.run([str(tree / "scripts" / "ears-two-mics.sh")], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "PATH": f"{tree / 'bin'}:{os.environ['PATH']}",
                                                "FAKE_SOURCES": SOURCES, **env})


def test_launcher_runs_with_two_listed_mics(tmp_path):
    tree = launcher_tree(tmp_path)
    r = run_launcher(tree, MIC1="MicYouVirtualSink.monitor", MIC2=USB)
    assert r.returncode == 0, r.stderr
    assert (tree / "bridge.in").read_text() == '{"type": "sentence"}\n'


def test_launcher_offers_a_phone_apps_monitor_source_but_not_the_speakers(tmp_path):
    # MicYou exposes the phone as MicYouVirtualSink.monitor: hiding every .monitor hid the phone mic.
    r = run_launcher(launcher_tree(tmp_path))
    assert r.returncode == 2
    assert "MicYouVirtualSink.monitor" in r.stderr and USB in r.stderr
    assert "Speaker__sink.monitor" not in r.stderr and "warning" not in r.stderr


def test_launcher_refuses_the_same_source_twice(tmp_path):
    r = run_launcher(launcher_tree(tmp_path), MIC1=USB, MIC2=USB)
    assert r.returncode == 2 and "different" in r.stderr
    assert not (tmp_path / "bridge.in").exists()


def test_launcher_refuses_a_source_that_is_not_there(tmp_path):
    r = run_launcher(launcher_tree(tmp_path), MIC1="MicYouVirtualSink.monitr", MIC2=USB)
    assert r.returncode == 2 and "MicYouVirtualSink.monitr" in r.stderr and "MicYouVirtualSink.monitor" in r.stderr
    assert not (tmp_path / "bridge.in").exists()


def test_launcher_refuses_the_speakers_monitor_as_a_mic(tmp_path):
    r = run_launcher(launcher_tree(tmp_path), MIC1="alsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink.monitor",
                     MIC2=USB)
    assert r.returncode == 2 and "speakers" in r.stderr


def test_launcher_warns_when_the_laptop_plays_into_the_phone_mics_sink(tmp_path):
    # The default sink is what the dashboard's voice and the dance music play to: into MicYou's sink, mic1 hears them.
    r = run_launcher(launcher_tree(tmp_path, default_sink="MicYouVirtualSink"), MIC1="MicYouVirtualSink.monitor",
                     MIC2=USB)
    assert r.returncode == 0, r.stderr
    assert "set-default-sink" in r.stderr


def test_launcher_runs_once(tmp_path):
    # A forgotten second launcher would send every sentence twice.
    import os
    import signal
    import subprocess
    tree = launcher_tree(tmp_path, ears_body="sleep 30")
    env = {**os.environ, "PATH": f"{tree / 'bin'}:{os.environ['PATH']}", "FAKE_SOURCES": SOURCES,
           "MIC1": "MicYouVirtualSink.monitor", "MIC2": USB}
    first = subprocess.Popen([str(tree / "scripts" / "ears-two-mics.sh")], env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_until(lambda: (tree / "bridge.in").exists(), timeout=10)
        r = run_launcher(tree, timeout=8, MIC1="MicYouVirtualSink.monitor", MIC2=USB)
        assert r.returncode != 0 and "already running" in r.stderr
    finally:
        os.killpg(first.pid, signal.SIGTERM)
        first.wait(10)
