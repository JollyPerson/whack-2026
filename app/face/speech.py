"""The referee's voice and the Mate Engine avatar bridge. Owner: D. Included by routes.py, so these work with
FACE=off too; FACE=real (run.py) adds pre-synthesis and the avatars' status dots.

GET  /face/tts?text=        the line as a cached 24 kHz mono WAV (503: no server voice; 504: not ready within
                            TTS_MISS_WAIT_S; 502: every provider failed). The browser falls back to its own speech.
GET  /face/mate/next        long-poll for the Mate Engine bridge: bus events -> bubble/mouth commands
                            (protocol: docs/INTEGRATION.md, D section). ME is a pure consumer: it publishes nothing.
GET  /face/mate/next/{ch}   the same, one avatar per channel: referee (= /face/mate/next), for, against (the side
                            bots' text-only bubbles, and their verdict dances on DebateResult). Any other channel is
                            a 404.
POST /face/mate/dance       {"title": "", "channel": "", "on": false, "seconds": 0} queues a dance ("" = the first
POST /face/mate/dance/stop  one; channel "" = every avatar; on = with its music; seconds 0 = to the end). Operator
                            only: not a bus event, so it is neither logged nor replayed.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
import weakref

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.brain import voice
from app.face.tts import TTS, TTSError, build_tts

router = APIRouter()

SAY_TTL_S = 4.0        # an Interjection or BotLine older than this at delivery is not shown (ME was away, or history)
DANCE_TTL_S = 10.0     # a queued dance nobody collected in this long is dropped
MAX_WAIT_S = 25
MATE_DOWN_S = 5.0      # no poll for this long = the avatar is gone (run.py turns its dot red)
FULL_DANCE_S = 300.0   # a verdict dance with seconds 0 plays to its end: about the longest one
TYPES = {"Interjection", "TTSState", "Control", "SessionStart", "BotLine", "BotTalk", "DebateResult"}
CHANNELS = ("referee", "for", "against")   # one avatar each; for/against = BotLine.persona
CITE_MAX_WORDS = 8     # a sourced bot line's second bubble line, "— the ONS: Labour market overview", in words
CITE_MAX_CHARS = 60    # and in characters (a title of long words)


def cmd(op: str, **kw) -> dict:
    """Every field always present, never null: Unity's JsonUtility can't handle nulls or missing keys. seconds: a
    dance's auto-stop (0 = to the end), always a whole number so an int or a float field on the Unity side reads it."""
    return {"op": op, "id": "", "text": "", "audio": "", "title": "", "on": False, "clear": False, "seconds": 0, **kw}


def whole_seconds(s: float) -> int:
    return math.ceil(max(0.0, float(s or 0)))      # 0.4 s is 1 s, never 0 (= to the end)


class Mate:
    """One avatar channel: its long-polls, liveness and dance queue."""

    def __init__(self):
        self.polls, self.seen = 0, 0.0         # long-polls in progress; monotonic time ME was last seen
        self.up: bool | None = None            # last published status (None = never connected)
        self.dances: list[tuple[float, dict]] = []
        self.dance_ready = asyncio.Event()
        self.dancing_until = 0.0               # wall time: a verdict dance is on until then (cleared by SessionStart)

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


def to_cmds(ev, tts: TTS, now: float, channel: str = "referee", mate: Mate | None = None) -> list[dict]:
    if channel != "referee":                   # a side bot: only its own lines, never the referee's
        return bot_cmds(ev, now, channel, mate)
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


def bot_cmds(ev, now: float, persona: str, mate: Mate | None = None) -> list[dict]:
    """A side bot's channel. Text only: BotLine opens the mouth while the bubble types, BotTalk(on=false) closes it.
    DebateResult: the bot's verdict dance (mate, when given, remembers it: a say would stop the dance in Mate Engine,
    so the bot's lines are dropped until it is over or the next SessionStart)."""
    if ev.event == "BotLine" and ev.persona == persona:
        if now - ev.ts > SAY_TTL_S or (mate is not None and now < mate.dancing_until):
            return []
        return [cmd("say", id=ev.id, text=bot_text(ev)), cmd("talk", id=ev.id, on=True)]
    if ev.event == "BotTalk" and ev.persona == persona:
        return [cmd("talk", id=ev.line_id, on=bool(ev.on))]
    if ev.event == "DebateResult":
        return verdict_dance(ev, now, persona, mate)
    if ev.event == "SessionStart":
        if mate is not None:
            mate.dancing_until = 0.0
        return [cmd("stop", clear=True), cmd("dance_stop")]
    if ev.event == "Control" and (
            (ev.flag == "mute" and ev.value is True) or (ev.flag == "bots" and ev.value is False)):
        return [cmd("stop", clear=True)]
    return []


def bot_text(ev) -> str:
    """What a bot's bubble shows: BotLine.text, plus, for a sourced line, a short second line naming its source like
    the stage characters' signs ("— the ONS: Labour market overview"). An unsourced line is its text alone (a
    challenge or concede already ends with "(unsourced)"). BotLine.text itself is never changed."""
    line = cite(ev.evidence) if ev.sourced else ""
    return f"{ev.text}\n{line}" if line else ev.text


def _short_title(title) -> str:
    """The page title without its site suffix ("Labour market overview - ONS" -> "Labour market overview"); "" for
    a title with a judgement word or a link in it (plain text only: the bubble never shows a URL)."""
    t = " ".join(str(title or "").split())
    for sep in (" | ", " - ", " — ", " – "):
        if sep in t:
            head, site = t.rsplit(sep, 1)
            if head.strip() and 0 < len(site.split()) <= 4:
                t = head
            break
    t = voice.clean(t)
    return "" if re.search(r"https?://|www\.|\.[a-z]{2,}/", t, re.I) else t


def cite(evidence) -> str:
    """"— <source name>: <short title>" for the first evidence item, at most CITE_MAX_WORDS words and CITE_MAX_CHARS
    characters (a longer title is cut with "…"); "— <name>" or "— <title>" when only one exists; "" when neither."""
    name = voice.source_name(evidence or [])
    titles = (_short_title(e.get("title") if isinstance(e, dict) else getattr(e, "title", "")) for e in evidence or [])
    title = next((t for t in titles if t), "")
    if title.lower() == name.lower():
        title = ""
    elif name and title.lower().startswith(name.lower()):
        name = ""                              # the name came from the title's first words: say the title once
    if not (name or title):
        return ""
    if not title:
        return _fit(f"— {name}")
    words, room = title.split(), max(1, CITE_MAX_WORDS - len(name.split()))
    short = " ".join(words[:room]).rstrip(" ,;:-") + ("…" if len(words) > room else "")
    return _fit(f"— {name}: {short}" if name else f"— {short}")


def _fit(line: str) -> str:
    """At most CITE_MAX_CHARS characters, cut at a word where there is one, with "…"."""
    if len(line) <= CITE_MAX_CHARS:
        return line
    cut = line[:CITE_MAX_CHARS - 1].rstrip("…")
    if cut.rfind(" ") > CITE_MAX_CHARS // 2:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


def verdict_dance(ev, now: float, persona: str, mate: Mate | None) -> list[dict]:
    """The winner's bot and the loser's dance their titles (a draw: both the same); music only where music[persona].
    No title for this bot, or a result older than its dance (Mate Engine was away): nothing."""
    title, seconds = str((ev.dances or {}).get(persona) or ""), whole_seconds(ev.dance_s)
    span = seconds or FULL_DANCE_S
    if not title or now - ev.ts > span:
        return []
    if mate is not None:
        mate.dancing_until = max(mate.dancing_until, now + span)
    return [cmd("dance", title=title, on=bool((ev.music or {}).get(persona)), seconds=seconds)]


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
            cmds = [c for e in evs for c in to_cmds(e, st.tts, time.time(), channel, mate)] + mate.take_dances()
            deadline = time.monotonic() + max(0, min(wait, MAX_WAIT_S))
            while not cmds and (left := deadline - time.monotonic()) > 0:
                ev = await _next(sub, mate, left, gone)
                if gone.done():
                    break                          # nobody to deliver to: queued dances wait for the next poll
                evs = ([ev] if ev else []) + [sub.q.get_nowait() for _ in range(sub.q.qsize())]
                cmds = [c for e in evs for c in to_cmds(e, st.tts, time.time(), channel, mate)] + mate.take_dances()
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
    on: bool = False       # with the dance's music (it reaches the mics: only outside the live debate)
    seconds: float = Field(0, ge=0, le=3600)   # stop after this long; 0 = play to the end


class DanceStop(BaseModel):
    channel: str = ""


def _queue(req: Request, c: dict, channel: str) -> dict:
    st = state_for(req.app.state.bus)
    mates = [st.mates[_channel(channel)]] if channel else list(st.mates.values())
    for m in mates:
        m.queue(c)
        if c["op"] == "dance_stop":
            m.dancing_until = 0.0      # the verdict dance is over: the bot's lines are no longer held back
    return {"ok": True, "mate": any(m.alive(time.monotonic()) for m in mates)}


@router.post("/face/mate/dance")
async def mate_dance(req: Request, body: Dance | None = None):
    body = body or Dance()
    return _queue(req, cmd("dance", title=body.title, on=body.on, seconds=whole_seconds(body.seconds)), body.channel)


@router.post("/face/mate/dance/stop")
async def mate_dance_stop(req: Request, body: DanceStop | None = None):
    return _queue(req, cmd("dance_stop"), (body or DanceStop()).channel)
