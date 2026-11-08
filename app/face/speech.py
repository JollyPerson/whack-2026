"""The referee's voice and the Mate Engine avatar bridge. Owner: D. Included by routes.py, so these work with
FACE=off too; FACE=real (run.py) adds pre-synthesis and the avatars' status dots.

GET  /face/tts?text=        the line as a cached 24 kHz mono WAV (503: no server voice; 504: not ready within
                            TTS_MISS_WAIT_S; 502: every provider failed). The browser falls back to its own speech.
GET  /face/mate/next        long-poll for the Mate Engine bridge: bus events -> bubble/mouth commands
                            (protocol: docs/INTEGRATION.md, D section). ME is a pure consumer: it publishes nothing.
GET  /face/mate/next/{ch}   the same, one avatar per channel: referee (= /face/mate/next), for, against (the side
                            bots' text-only bubbles). Any other channel is a 404.
POST /face/mate/dance       {"title": "", "channel": ""} queues a dance ("" = the first one; channel "" = every
POST /face/mate/dance/stop  avatar). Operator only: not a bus event, so it is neither logged nor replayed.
"""
from __future__ import annotations

import asyncio
import time
import uuid
import weakref

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.face.tts import TTS, TTSError, build_tts

router = APIRouter()

SAY_TTL_S = 4.0        # an Interjection or BotLine older than this at delivery is not shown (ME was away, or history)
DANCE_TTL_S = 10.0     # a queued dance nobody collected in this long is dropped
MAX_WAIT_S = 25
MATE_DOWN_S = 5.0      # no poll for this long = the avatar is gone (run.py turns its dot red)
TYPES = {"Interjection", "TTSState", "Control", "SessionStart", "BotLine", "BotTalk"}
CHANNELS = ("referee", "for", "against")   # one avatar each; for/against = BotLine.persona


def cmd(op: str, **kw) -> dict:
    """Every field always present, never null: Unity's JsonUtility can't handle nulls or missing keys."""
    return {"op": op, "id": "", "text": "", "audio": "", "title": "", "on": False, "clear": False, **kw}


class Mate:
    """One avatar channel: its long-polls, liveness and dance queue."""

    def __init__(self):
        self.polls, self.seen = 0, 0.0         # long-polls in progress; monotonic time ME was last seen
        self.up: bool | None = None            # last published status (None = never connected)
        self.dances: list[tuple[float, dict]] = []
        self.dance_ready = asyncio.Event()

    def alive(self, now: float) -> bool:
        return self.polls > 0 or (self.seen > 0 and now - self.seen < MATE_DOWN_S)

    def queue(self, c: dict) -> None:
        now = time.monotonic()                 # expired ones go now: a channel nobody polls doesn't pile up
        self.dances = [(t, d) for t, d in self.dances if now - t <= DANCE_TTL_S] + [(now, c)]
        self.dance_ready.set()

    def take_dances(self) -> list[dict]:
        now, queued = time.monotonic(), self.dances
        self.dances = []
        self.dance_ready.clear()
        return [c for t, c in queued if now - t <= DANCE_TTL_S]


class FaceState:
    """Per bus (= per server process; per test client in tests)."""

    def __init__(self):
        self.epoch = uuid.uuid4().hex[:8]      # changes on restart: ME resyncs instead of trusting its old cursor
        self.tts: TTS = build_tts()
        self.mates = {ch: Mate() for ch in CHANNELS}


_STATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def state_for(bus) -> FaceState:
    st = _STATES.get(bus)
    if st is None:
        st = _STATES[bus] = FaceState()
    return st


def to_cmds(ev, tts: TTS, now: float, channel: str = "referee") -> list[dict]:
    if channel != "referee":                   # a side bot: only its own lines, never the referee's
        return bot_cmds(ev, now, channel)
    if ev.event == "Interjection":
        return [] if now - ev.ts > SAY_TTL_S else [cmd("say", id=ev.id, text=ev.text, audio=tts.url_for(ev.text))]
    if ev.event == "TTSState":                 # the ?audio=1 tab is the one player: the mouth follows it, never a
        if ev.src.split(":")[0] == "replay":   # recorded tab's TTSState (./run.sh replay of a log made with one)
            return []
        return [cmd("talk", id=ev.interjection_id or "", on=bool(ev.speaking))]
    if ev.event == "SessionStart":
        return [cmd("stop", clear=True)]
    if ev.event == "Control":
        if (ev.flag == "mute" and ev.value is True) or (ev.flag == "director" and ev.value is False):
            return [cmd("stop", clear=True)]
        if ev.flag == "tts" and ev.value is False:
            return [cmd("stop")]               # voice off: close the mouth, keep the bubble
    return []


def bot_cmds(ev, now: float, persona: str) -> list[dict]:
    """A side bot's channel. Text only: BotLine opens the mouth while the bubble types, BotTalk(on=false) closes it."""
    if ev.event == "BotLine" and ev.persona == persona:
        return [] if now - ev.ts > SAY_TTL_S else [cmd("say", id=ev.id, text=ev.text), cmd("talk", id=ev.id, on=True)]
    if ev.event == "BotTalk" and ev.persona == persona:
        return [cmd("talk", id=ev.line_id, on=bool(ev.on))]
    if ev.event == "SessionStart" or (ev.event == "Control" and (
            (ev.flag == "mute" and ev.value is True) or (ev.flag == "bots" and ev.value is False))):
        return [cmd("stop", clear=True)]
    return []


async def _gone(req: Request) -> None:
    """Returns once the client has hung up (Mate Engine killed, or its request timed out). The server doesn't cancel
    a handler on disconnect, so without this a dead poll would hold its subscription and take queued dances."""
    while (await req.receive())["type"] != "http.disconnect":
        pass


async def _next(sub, mate: Mate, timeout: float, gone: asyncio.Future):
    """The next bus event, or None when a dance was queued, the client hung up or the time is up."""
    get, dance = asyncio.ensure_future(sub.q.get()), asyncio.ensure_future(mate.dance_ready.wait())
    try:
        done, _ = await asyncio.wait({get, dance, gone}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        get.cancel()
        dance.cancel()
    return get.result() if get in done else None


@router.get("/face/tts")
async def tts_wav(req: Request, text: str = Query(..., min_length=1, max_length=600)):
    tts = state_for(req.app.state.bus).tts
    if not tts.providers:
        raise HTTPException(503, "no server voice: set ELEVENLABS_API_KEY or install espeak-ng (see TTS_PROVIDER)")
    try:
        path = await tts.wav(text)
    except TTSError as e:
        raise HTTPException(502, str(e))
    if path is None:
        raise HTTPException(504, f"not synthesised within {tts.miss_wait:g} s (it is cached when it lands)")
    return FileResponse(path, media_type="audio/wav")


def _channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise HTTPException(404, f"unknown avatar channel: use one of {', '.join(CHANNELS)}")
    return channel


@router.get("/face/mate/next")
async def mate_next(req: Request, after: int = -1, epoch: str = "", wait: int = 10):
    return await _poll(req, "referee", after, epoch, wait)


@router.get("/face/mate/next/{channel}")
async def mate_next_channel(req: Request, channel: str, after: int = -1, epoch: str = "", wait: int = 10):
    return await _poll(req, _channel(channel), after, epoch, wait)


async def _poll(req: Request, channel: str, after: int, epoch: str, wait: int) -> dict:
    bus = req.app.state.bus
    st = state_for(bus)
    mate = st.mates[channel]
    mate.polls += 1
    mate.seen = time.monotonic()
    try:
        if after < 0 or epoch != st.epoch or after > bus.last_seq:   # first poll, or the server restarted:
            return {"v": 1, "epoch": st.epoch, "cursor": bus.last_seq, "cmds": []}   # resync, never replay
        name = "face:mate" if channel == "referee" else f"face:mate:{channel}"
        sub = bus.subscribe(name, TYPES)       # subscribe + scan history in one tick: nothing falls between
        gone = asyncio.ensure_future(_gone(req))
        try:
            evs = sorted((e for e in bus.history if e.seq > after and e.event in TYPES), key=lambda e: e.seq)
            cmds = [c for e in evs for c in to_cmds(e, st.tts, time.time(), channel)] + mate.take_dances()
            deadline = time.monotonic() + max(0, min(wait, MAX_WAIT_S))
            while not cmds and (left := deadline - time.monotonic()) > 0:
                ev = await _next(sub, mate, left, gone)
                if gone.done():
                    break                          # nobody to deliver to: queued dances wait for the next poll
                evs = ([ev] if ev else []) + [sub.q.get_nowait() for _ in range(sub.q.qsize())]
                cmds = [c for e in evs for c in to_cmds(e, st.tts, time.time(), channel)] + mate.take_dances()
            return {"v": 1, "epoch": st.epoch, "cursor": bus.last_seq, "cmds": cmds}   # no await since the drain
        finally:
            gone.cancel()
            sub.close()
    finally:
        mate.polls -= 1
        mate.seen = time.monotonic()


class Dance(BaseModel):
    title: str = ""
    channel: str = ""      # one avatar (referee | for | against), or "" for every one


class DanceStop(BaseModel):
    channel: str = ""


def _queue(req: Request, c: dict, channel: str) -> dict:
    st = state_for(req.app.state.bus)
    mates = [st.mates[_channel(channel)]] if channel else list(st.mates.values())
    for m in mates:
        m.queue(c)
    return {"ok": True, "mate": any(m.alive(time.monotonic()) for m in mates)}


@router.post("/face/mate/dance")
async def mate_dance(req: Request, body: Dance | None = None):
    body = body or Dance()
    return _queue(req, cmd("dance", title=body.title), body.channel)


@router.post("/face/mate/dance/stop")
async def mate_dance_stop(req: Request, body: DanceStop | None = None):
    return _queue(req, cmd("dance_stop"), (body or DanceStop()).channel)
