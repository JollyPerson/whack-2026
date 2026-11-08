"""Mock roles (mock_ears/mock_brain/mock_evidence) and config tunables: hardening regressions."""
from __future__ import annotations

import asyncio
import json

from app import config
from app.bus import Bus
from app.mocks import mock_brain, mock_ears, mock_evidence
from app.schemas import Claim, Pause, SessionStart, Verdict


def drain(sub) -> list:
    return [sub.q.get_nowait() for _ in range(sub.q.qsize())]


def contradicted_text() -> str:
    """A checkable claim text the mock evidence always labels 'contradicted' (label = hash % 4)."""
    return next(t for t in (f"Bus use fell by {i} percent." for i in range(100))
                if mock_evidence.LABELS[mock_evidence._h(t) % 4] == "contradicted")


async def test_mock_ears_non_positive_speed_env_falls_back_to_real_time(tmp_path, monkeypatch):
    """SPEED=0 divided by zero on the first event; the supervisor then crash-looped mock ears forever."""
    p = tmp_path / "f.jsonl"
    p.write_text(json.dumps({"event": "Utterance", "id": "u1", "speaker": "A", "text": "one two", "t_start": 0.1,
                             "t_end": 0.2, "ts": 0.3}) + "\n")
    monkeypatch.setenv("MOCK_EARS_DELAY", "0")
    monkeypatch.delenv("LOOP", raising=False)
    for bad in ("0", "-3"):
        monkeypatch.setenv("SPEED", bad)
        b = Bus()
        s = b.subscribe("t", {"Utterance"})
        await asyncio.wait_for(mock_ears.run(b, p), 2)
        assert [e.id for e in drain(s)] == ["u1"]


def test_env_parses_float_spelled_ints_and_warns_on_values_it_cannot_use(monkeypatch, capsys):
    """DIRECTOR_AGGR=4.0 silently stayed 3 and DIRECTOR_MIN_GAP_S=20s silently stayed 30 during venue tuning."""
    for name, raw, default, want in (("X_INT", "4.0", 3, 4), ("X_INT2", " 8 ", 6, 8), ("X_F", "20", 30.0, 20.0),
                                     ("X_B", "yes", False, True), ("X_S", "claude haiku", "x", "claude haiku")):
        monkeypatch.setenv(name, raw)
        assert config._env(name, default) == want and type(config._env(name, default)) is type(default)
    assert capsys.readouterr().err == ""
    for name, raw, default, want in (("X_F", "20s", 30.0, 30.0), ("X_INT", "4.5", 3, 3), ("X_INT", "inf", 3, 3),
                                     ("X_B", "ture", False, False)):
        monkeypatch.setenv(name, raw)
        assert config._env(name, default) == want
        err = capsys.readouterr().err
        assert name in err and repr(raw) in err and repr(want) in err, err
    monkeypatch.setenv("X_B", "off")
    assert config._env("X_B", True) is False and capsys.readouterr().err == ""
    monkeypatch.setenv("X_F", "")                                                  # empty = not set: no warning
    assert config._env("X_F", 30.0) == 30.0 and capsys.readouterr().err == ""
