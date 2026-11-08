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

with mock.patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=object)}):
    _spec = importlib.util.spec_from_file_location("ears_stt", ROOT / "ears" / "ears.py")
    ears = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ears)


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
