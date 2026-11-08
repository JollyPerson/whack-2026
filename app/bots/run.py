"""The side bots, wired onto the bus. Owner: B. Runs only with BOTS=real (no profile starts it).

Consumes: SessionStart, Claim, Verdict, ArgMapSnapshot, Control, BotLine
Produces: BotLine, BotTalk (src="bots"), ModuleStatus(bots)

The For bot is Prop's second and answers Opp's claims; the Against bot mirrors it (Claim.side, set by the brain).
A claim with no side is ignored, and a bot never answers its own side. Per opponent claim:
- checkable and important: Claude's own-knowledge check starts at once (hiding its latency) while the bot waits up
  to BOTS_VERDICT_WAIT_S for the claim's Verdict. A sourced Verdict (lines.sourced) -> "rebut" quoting its source.
  Supported -> no fact line. Otherwise a check that says likely false with confidence >= BOTS_CHECK_MIN_CONF ->
  "challenge", tagged "(unsourced)". At most one fact line per claim.
- no fact line and important -> "followup": a pointed question (Claude, else a template by claim type).
- not checkable but important (arguments, predictions) -> "followup" straight away.
Nudges: when one of a bot's OWN side's points turns unanswered in the ArgMapSnapshot (or is listed as ignored by a
non-responsive turn), that bot nudges the opponent: "Still waiting on the cost point."
Banter (BOTS_BANTER): after one bot posts a non-banter line, at most once per BOTS_BANTER_GAP_S across both, the
other bot may answer it with one dry line, if it is idle (banter never delays a real line). Banter is never answered.
Control bots=false or mute=true pauses the bots (queues and in-flight work dropped); SessionStart resets everything.
A crash-restart rebuilds what was already said from the bus history, silently.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from app import config
from app.bots import lines
from app.bots.pacer import Pacer
from app.bots.writer import Writer
from app.brain.director import truthy
from app.schemas import (ArgMapSnapshot, BotLine, Claim, Control, Event, ModuleStatus, SessionStart, Verdict)

log = logging.getLogger("bots")
TICK_S = 0.25
BANTER_TTL_S = 10.0       # a banter reply that can't post within this is stale
BANTER_SOON_S = 3.0       # banter only goes to a bot that is free within this and has nothing queued


class Bots:
    """All the side bots' state. handle() takes bus events, tick() publishes what the pacer releases; per-claim work
    runs as tasks. `clock` is the pacer's clock (time.time; tests pass a fake one)."""

    def __init__(self, publish: Callable[[Event], Any], writer: Optional[Writer] = None,
                 clock: Callable[[], float] = time.time):
        self.publish, self.writer, self.clock = publish, writer or Writer(), clock
        self.pacer = Pacer(config.BOTS_MIN_GAP_S, config.BOTS_LINE_TTL_S, BANTER_TTL_S)
        self.enabled = truthy(config.FLAGS.get("bots", True))
        self.muted = truthy(config.FLAGS.get("mute", False))
        self.pacer.paused = self.paused
        self.tasks: set[asyncio.Task] = set()
        self.reset()

    @property
    def paused(self) -> bool:
        return not self.enabled or self.muted

    def reset(self, ev: Optional[SessionStart] = None) -> None:
        self.cancel()
        self.session = getattr(self, "session", 0) + 1
        self.pacer.min_gap, self.pacer.ttl = config.BOTS_MIN_GAP_S, config.BOTS_LINE_TTL_S
        self.pacer.reset()
        self.motion = ev.motion if ev else ""
        self.sides: dict[str, str] = {}               # speaker -> side
        self.names: list[str] = []                    # debaters' names: never in a bot line
        for spk, info in (ev.speakers.items() if ev else []):
            if info.side in ("prop", "opp"):
                self.sides[spk] = info.side
            self.names += [w for w in info.name.split() if len(w) > 2]
        self.last_speaker: dict[str, str] = {}        # side -> who spoke for it last
        self.claims: dict[str, Claim] = {}
        self.verdicts: dict[str, Verdict] = {}
        self.waiting: dict[str, asyncio.Event] = {}
        self.statuses: dict[str, str] = {}            # claim id -> node status in the last ArgMapSnapshot
        self.map_version = -1
        self.nudged: set[str] = set()
        self.said: list[str] = []                     # texts posted this session, newest last (LLM "don't repeat")
        self.banters: list[str] = []                  # the banter among them
        self.last_banter = -1e18
        self.banter_busy = False

    def cancel(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        self.tasks.clear()

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)                             # also keeps it from being garbage-collected mid-flight
        t.add_done_callback(self.tasks.discard)
        return t

    # ------------------------------------------------------------------------------------------- events
    def handle(self, ev: Event) -> None:
        name = ev.event
        if name == "SessionStart":
            self.reset(ev)
        elif name == "Control":
            self.on_control(ev)
        elif name == "Verdict":
            self.verdicts.setdefault(ev.claim_id, ev)    # the first verdict decides: one fact line per claim
            if (w := self.waiting.get(ev.claim_id)) is not None:
                w.set()
        elif name == "Claim":
            self.on_claim(ev)
        elif name == "ArgMapSnapshot":
            self.on_snapshot(ev)
        elif name == "BotLine":
            self.on_botline(ev)

    def on_control(self, ev: Control) -> None:
        if ev.flag == "bots":
            self.enabled = truthy(ev.value)
        elif ev.flag == "mute":
            self.muted = truthy(ev.value)
        else:
            return
        if self.paused and not self.pacer.paused:
            self.cancel()
            self.banter_busy = False                  # a banter task cancelled before it started never resets it
            for talk in self.pacer.clear():           # close any open mouth now
                self.publish(talk)
        self.pacer.paused = self.paused

    def remember(self, c: Claim) -> Optional[str]:
        """Track the claim's speaker and side; returns the persona that answers it (None: ignore it)."""
        side = str(c.side or "").lower()
        if side in ("prop", "opp"):
            self.sides.setdefault(c.speaker, side)
            self.last_speaker[side] = c.speaker
        first = c.id not in self.claims
        self.claims[c.id] = c
        return lines.answering(side) if first else None

    def on_claim(self, c: Claim) -> None:
        persona = self.remember(c)
        if persona is None or self.paused:
            return
        dup = c.canonical_key and any(o.canonical_key == c.canonical_key and o.id != c.id and o.side == c.side
                                      for o in self.claims.values())
        if dup:                                       # the same side said it again: answered the first time
            return
        self.spawn(self.answer(c, persona, self.session))

    # ------------------------------------------------------------------------------------------- claims
    async def wait_verdict(self, cid: str, wait: float) -> Optional[Verdict]:
        if cid not in self.verdicts and wait > 0:
            ev = self.waiting.setdefault(cid, asyncio.Event())
            try:
                await asyncio.wait_for(ev.wait(), wait)
            except TimeoutError:
                pass
            finally:
                if self.waiting.get(cid) is ev:      # a cancelled task of an earlier session leaves the new one alone
                    del self.waiting[cid]
        return self.verdicts.get(cid)

    async def answer(self, c: Claim, persona: str, session: int) -> None:
        try:
            await self._answer(c, persona, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bots: answering %s failed", c.id)

    async def _answer(self, c: Claim, persona: str, session: int) -> None:
        checkable = c.checkable and c.type not in lines.NO_CHECK
        important = c.importance >= lines.IMPORTANT
        if not checkable and not important:
            return
        check = None
        if checkable and important and self.writer.checker is not None:
            check = self.spawn(self.writer.check(c, self.motion))
        verdict = await self.wait_verdict(c.id, config.BOTS_VERDICT_WAIT_S) if checkable else None
        granted = lines.supported(verdict)
        if verdict is not None and lines.sourced(verdict):
            text = lines.rebut(c, verdict, self.said)
            if text:
                if check:
                    check.cancel()
                self.queue(session, persona=persona, text=text, kind="rebut", target=c.speaker, claim_id=c.id,
                           sourced=True, evidence=list(verdict.evidence))
                return
        if check is not None:
            res = None if granted else await check
            check.cancel()
            if res and res.likely_false and res.confidence >= config.BOTS_CHECK_MIN_CONF:
                text = lines.challenge(c, res.challenge, res.correction)
                if text and self.queue(session, persona=persona, text=text, kind="challenge", target=c.speaker,
                                       claim_id=c.id):
                    return
        if not important:
            return
        raw = await self.writer.followup(c, self.motion, persona, granted, tuple(self.said))
        text = lines.guard(raw, "followup", c.text, strict=True, names=self.names) if raw else None
        if raw and not text:
            log.info("bots: follow-up dropped by the guards: %r", raw)
        if not text or self.pacer.has_said(text):
            text = lines.followup(c, self.said, granted)
        if text:
            self.queue(session, persona=persona, text=text, kind="followup", target=c.speaker, claim_id=c.id)

    def queue(self, session: int, **kw) -> bool:
        """Hand a line to the pacer (and post it now if its bot is free). False if dropped."""
        if session != self.session or self.paused:
            return False
        line = BotLine(hold_s=lines.hold_s(kw["text"]), **kw)
        ok = self.pacer.push(line, self.clock())
        self.tick()
        return ok

    # ------------------------------------------------------------------------------------------- nudges
    def opponent_of(self, side: str) -> Optional[str]:
        """Who speaks for the other side: the last one heard, else the only one known."""
        other = "opp" if side == "prop" else "prop"
        if other in self.last_speaker:
            return self.last_speaker[other]
        known = [s for s, sd in self.sides.items() if sd == other]
        return known[0] if len(known) == 1 else None

    def on_snapshot(self, snap: ArgMapSnapshot) -> None:
        if snap.version <= self.map_version:
            return
        self.map_version = snap.version
        for spk, side in (getattr(snap, "speakers", None) or {}).items():
            if side in ("prop", "opp"):
                self.sides.setdefault(spk, side)
        nodes = {n.claim_id: n for n in snap.nodes}
        due: dict[str, tuple] = {}                     # persona -> (importance, node, target)
        for n in snap.nodes:
            if n.status == "unanswered" and self.statuses.get(n.claim_id) != "unanswered" and n.side in lines.SECOND:
                self._consider(due, lines.SECOND[n.side], n, self.opponent_of(n.side))
        for nr in snap.nonresponsive or []:
            persona = lines.answering(nr.get("side"))     # the side that ignored our points: answered by our bot
            for cid in nr.get("ignored") or []:
                n = nodes.get(cid)
                if persona and n is not None and n.side == lines.OWN_SIDE[persona]:
                    self._consider(due, persona, n, nr.get("speaker"))
        self.statuses = {n.claim_id: n.status for n in snap.nodes}
        if self.paused:
            return
        for persona, (_, n, target) in due.items():
            self.nudged.add(n.claim_id)
            topic = getattr(n, "topic", "") or getattr(self.claims.get(n.claim_id), "topic", "")
            text = lines.nudge(topic, n.text, self.said)
            if text:
                self.queue(self.session, persona=persona, text=text, kind="nudge", target=target, claim_id=n.claim_id)

    def _consider(self, due: dict, persona: str, n: Any, target: Optional[str]) -> None:
        if n.claim_id in self.nudged:
            return
        if persona not in due or n.importance > due[persona][0]:
            due[persona] = (n.importance, n, target)

    # ------------------------------------------------------------------------------------------- banter
    def idle(self, persona: str, within: float = 0.0) -> bool:
        """Free to post within `within` seconds, with nothing of its own waiting (banter never displaces a line)."""
        return self.pacer.free(persona, self.clock() + within) and not self.pacer.queued(persona)

    def on_botline(self, line: BotLine) -> None:
        if line.src != "bots" or line.kind == "banter" or line.reply_to:
            return                                    # never chained; replayed or foreign lines don't count
        if not config.BOTS_BANTER or self.paused or self.writer.fast is None or self.banter_busy:
            return
        other = lines.OTHER.get(line.persona)
        if not other or not self.idle(other, BANTER_SOON_S) or \
                self.clock() - self.last_banter < config.BOTS_BANTER_GAP_S:
            return
        prev, self.last_banter, self.banter_busy = self.last_banter, self.clock(), True   # reserves the gap
        self.spawn(self.banter(line, other, prev, self.session))

    async def banter(self, line: BotLine, persona: str, prev: float, session: int) -> None:
        ok = False
        try:
            raw = await self.writer.banter(line.text, persona, line.persona, tuple(self.banters))
            text = lines.guard(raw, "banter", line.text, strict=True, names=self.names) if raw else None
            if raw and not text:
                log.info("bots: banter dropped by the guards: %r", raw)
            ok = bool(text) and self.idle(persona, BANTER_SOON_S) and \
                self.queue(session, persona=persona, text=text, kind="banter", reply_to=line.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bots: banter failed")
        finally:
            if session == self.session:
                self.banter_busy = False
                if not ok:
                    self.last_banter = prev           # nothing queued: the gap doesn't start yet

    # ------------------------------------------------------------------------------------------- output
    def tick(self) -> None:
        for ev in self.pacer.tick(self.clock()):
            if isinstance(ev, BotLine):
                self.said.append(ev.text)
                if ev.kind == "banter":
                    self.last_banter = self.clock()
                    self.banters.append(ev.text)
            self.publish(ev)

    def restore(self, history: list[Event]) -> None:
        """After a crash-restart: the session, claims, verdicts, map statuses and lines already posted, silently."""
        for ev in history:
            try:
                if ev.event == "SessionStart":
                    self.reset(ev)
                elif ev.event == "Claim":
                    self.remember(ev)
                elif ev.event == "Verdict":
                    self.verdicts.setdefault(ev.claim_id, ev)
                elif ev.event == "ArgMapSnapshot" and ev.version > self.map_version:
                    self.map_version, self.statuses = ev.version, {n.claim_id: n.status for n in ev.nodes}
                    self.nudged.update(c for nr in ev.nonresponsive for c in nr.get("ignored") or [])
                elif ev.event == "BotLine" and ev.src == "bots":
                    self.pacer.posted(ev, ev.ts)
                    self.said.append(ev.text)
                    if ev.kind == "nudge" and ev.claim_id:
                        self.nudged.add(ev.claim_id)
                    if ev.kind == "banter":
                        self.last_banter = ev.ts
                        self.banters.append(ev.text)
            except Exception:
                log.exception("bots: restore skipped %s", ev.event)


async def run(bus) -> None:
    from app.bots.writer import make_writer

    # Subscribe and snapshot the history in one synchronous step: every event is restored or handled live, once.
    sub = bus.subscribe("bots", {"SessionStart", "Claim", "Verdict", "ArgMapSnapshot", "Control", "BotLine"},
                        maxsize=512)
    history = list(bus.history)
    try:
        writer = await asyncio.to_thread(make_writer)   # SDK import (~1 s) off the shared event loop
    except asyncio.CancelledError:
        sub.close()
        raise
    except Exception:
        log.exception("bots: LLM client unavailable; templates only, no knowledge check")
        writer = Writer()
    bots = Bots(lambda ev: bus.publish(ev, src="bots"), writer)
    bots.restore(history)
    bus.publish(ModuleStatus(module="bots", state="up", detail=f"llm: {writer.name}"), src="bots")
    log.info("bots up: %s", writer.name)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                bots.tick()
            except Exception:
                log.exception("bots: tick failed")

    tick_task = asyncio.create_task(ticker())
    try:
        async for ev in sub:
            try:
                bots.handle(ev)
            except Exception:
                log.exception("bots failed on %s", ev.event)
    finally:
        tick_task.cancel()
        bots.cancel()
        sub.close()
