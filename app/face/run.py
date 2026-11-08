"""FACE=real: pre-synthesises the referee's lines and watches the Mate Engine avatar. Owner: D.

The routes in speech.py work without this (FACE=off). It adds two things:
- pre-synthesis (TTS_PRESYNTH): "lines" (default) synthesises every Interjection the moment it is published, so the
  ?audio=1 tab's /face/tts request is usually already in flight; "candidates" also synthesises Director candidate/held
  lines (DirectorDecision.text) ahead of time, which hides TTS latency but costs ~10x the characters (a free
  ElevenLabs plan is 10,000 characters a month); "off" synthesises only on request;
- the avatar's status dot: ModuleStatus(module="mate") goes up on its first poll and down after MATE_DOWN_S
  without one, published only on change (nothing at all if Mate Engine never connects).
Never returns.
"""
from __future__ import annotations

import asyncio
import os
import time

from app.config import FLAGS
from app.face.speech import MATE_DOWN_S, FaceState, state_for
from app.schemas import ModuleStatus

PRESYNTH_MODES = ("lines", "candidates", "off")


def presynth_mode() -> str:
    mode = os.environ.get("TTS_PRESYNTH", "lines").strip().lower()
    return mode if mode in PRESYNTH_MODES else "lines"


def line_of(ev, mode: str = "lines") -> str:
    if mode == "off":
        return ""
    if ev.event == "Interjection":
        return ev.text
    if mode == "candidates" and ev.event == "DirectorDecision" and ev.action in ("candidate", "held"):
        return ev.text
    return ""


def watch_mate(bus, st: FaceState, now: float) -> None:
    alive = st.mate_alive(now)
    if alive == st.mate_up or (st.mate_up is None and not alive):
        return
    st.mate_up = alive
    bus.publish(ModuleStatus(module="mate", state="up" if alive else "down",
                             detail="avatar polling /face/mate/next" if alive else
                             f"no poll for {MATE_DOWN_S:g} s: is Mate Engine running?"), src="face")


async def run(bus):
    st = state_for(bus)
    mode = presynth_mode()
    sub = bus.subscribe("face", {"DirectorDecision", "Interjection"})
    try:
        while True:
            try:
                ev = await asyncio.wait_for(sub.q.get(), 1.0)
            except TimeoutError:
                ev = None
            if ev is not None and (text := line_of(ev, mode)) and FLAGS.get("tts") is not False:
                st.tts.prefetch(text)
            watch_mate(bus, st, time.monotonic())
    finally:
        sub.close()
