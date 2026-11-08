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
