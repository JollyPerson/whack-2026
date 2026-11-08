"""Mock ears: replay a transcript fixture as if live. Owner: B (stand-in for A).

Emits an interim Utterance every 3 words during speech (unless the fixture has its own interims), the
final at its recorded `ts`, and the Pauses in the file. A SessionStart line in the fixture is published
too (motion + speaker sides). Only what ears produce is re-emitted, so a whole run log works as a fixture:
recorded Claims/Verdicts/Interjections and the brain's synthetic Pauses are dropped, only the last
session is used, and silences longer than FIXTURE_MAX_GAP seconds (default 5) are cut.
Env: FIXTURE (default fixtures/test_debate.jsonl, else fixtures/fake_debate.jsonl), SPEED, LOOP=1.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from app.bus import MAX_GAP_S, gap_offsets, last_session, load_jsonl, rebase
from app.schemas import Utterance

log = logging.getLogger("mock_ears")
ROOT = Path(__file__).resolve().parents[2]
SPEECH = ("SessionStart", "Utterance", "Pause")


def default_fixture() -> Path:
    env = os.environ.get("FIXTURE")
    if env:
        return Path(env)
    real = ROOT / "fixtures" / "test_debate.jsonl"
    return real if real.exists() else ROOT / "fixtures" / "fake_debate.jsonl"


def speech_only(events) -> list:
    """What ears (and the session form) produce; never the brain's synthetic Pauses (src brain:pause_synth)."""
    evs = [e for e in events if e.event in SPEECH and not any(p.startswith("brain") for p in e.src.split(":"))]
    return last_session(sorted(evs, key=lambda e: e.ts))


def schedule(events) -> list:
    events = speech_only(events)
    synth = not any(isinstance(e, Utterance) and not e.final for e in events)   # no recorded interims: make some
    sched = []
    for e in events:
        if synth and isinstance(e, Utterance) and e.final and 0 < e.t_end - e.t_start < 120:
            words = e.text.split()
            for k in range(3, len(words), 3):
                t = e.t_start + (e.t_end - e.t_start) * k / len(words)
                sched.append((t, e.model_copy(update={"text": " ".join(words[:k]), "final": False, "ts": t})))
        sched.append((e.ts, e))
    sched.sort(key=lambda x: x[0])
    return sched


async def run(bus, fixture: Path | None = None, speed: float | None = None):
    fixture = Path(fixture or default_fixture())
    if not speed:
        try:
            speed = float(os.environ.get("SPEED", "1"))
        except ValueError:
            speed = 0.0
    if not speed > 0:                      # 0 divided by zero on the first event (then a crash loop); < 0 ran backwards
        log.warning("speed %r (SPEED=%r) is not a number > 0: replaying at 1x", speed, os.environ.get("SPEED"))
        speed = 1.0
    max_gap = float(os.environ.get("FIXTURE_MAX_GAP", MAX_GAP_S))
    await asyncio.sleep(float(os.environ.get("MOCK_EARS_DELAY", "0.3")))   # let other modules subscribe first
    while True:
        sched = schedule(load_jsonl(fixture))
        if not sched:
            return
        t0, w0 = sched[0][0], time.time()
        for (t, e), off in zip(sched, gap_offsets([t for t, _ in sched], max_gap)):
            def shift(x: float, off: float = off) -> float:
                return w0 + (x - t0 - off) / speed

            d = shift(t) - time.time()
            if d > 0:
                await asyncio.sleep(d)
            ev = rebase(e.model_copy(), shift)
            ev.seq, ev.src = 0, "mock_ears"
            bus.publish(ev)
        if os.environ.get("LOOP") != "1":
            return
        await asyncio.sleep(3)
