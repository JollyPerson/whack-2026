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
