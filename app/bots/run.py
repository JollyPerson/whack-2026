"""The side bots, wired onto the bus. Owner: B. Runs only with BOTS=real (no profile starts it).

Consumes: SessionStart, Claim, Verdict, ArgMapSnapshot, Consistency, Control, BotLine
Produces: BotLine, BotTalk (src="bots"), ModuleStatus(bots)

The For bot is Prop's second and answers Opp's claims; the Against bot mirrors it (Claim.side, set by the brain).
A claim with no side is ignored, and a bot never answers its own side. Per opponent claim:
- checkable and important: Claude's own-knowledge check starts at once (hiding its latency) while the bot waits up
  to BOTS_VERDICT_WAIT_S for the claim's Verdict. A sourced Verdict (lines.sourced) -> "rebut" quoting its source.
  Supported -> no fact line. Otherwise the check's assessment decides, every line tagged "(unsourced)":
  likely_false -> "challenge" with Claude's correction; surprising_true (a well-known misconception) -> a grudging
  "concede", both only with confidence >= BOTS_CHECK_MIN_CONF; misleading (true figure, misrepresented) -> a
  "True, but ..." "challenge" from BOTS_MISLEADING_MIN_CONF, its own bar. A check that disagrees with the claim's
  Verdict label is dropped. At most one fact line per claim. A later Verdict for the same claim (Evidence's deep
  check, published only when it does better) replaces the earlier one, so it still counts while the bot is busy.
- no fact line and important -> "followup": a pointed question (Claude, else a template by claim type).
- not checkable but important (arguments, predictions) -> "followup" straight away.
Nudges: when one of a bot's OWN side's points turns unanswered in the ArgMapSnapshot (or is listed as ignored by a
finished non-responsive turn), that bot nudges the opponent: "Still waiting on the cost point."
The debate record (the brain's Consistency findings, already over CONSISTENCY_MIN_CONF), one line per finding:
- self_contradiction by side X -> the bot opposing X posts a "uturn" to X's speaker, quoting both claims verbatim
  from the finding: "Earlier: 'cut it by 6%'. Now: 'about 4%'. Which is it?"
- concession by side X -> the bot of the side whose point X granted posts "conceded": "Noted: you just conceded the
  cost point." Agreements and corrections get no line. Not AI knowledge: the debater's own words, so no tag.
Banter (BOTS_BANTER), lowest priority: after one bot posts a non-banter line, at most once per BOTS_BANTER_GAP_S
across both, the other bot may answer it with one dry line. It starts only when nothing important is running or
waiting: no claim being answered for either bot (its knowledge check, its Verdict wait or its line being written),
no non-banter line queued for either bot, and the replying bot idle; all of it is checked again when the line is
written, before it is queued. If that only comes true later, it may still start within BANTER_FRESH_S of the line.
A queued banter is dropped when a real line for its bot is ready first or a claim for its bot comes in, and then
gives the gap back. Banter is never answered.
Control bots=false or mute=true pauses the bots (queues and in-flight work dropped); so does the final screen
(Control screen=final), until screen=live or the next SessionStart, so no bubble cuts into the verdict and its
dances. SessionStart resets everything.
A crash-restart rebuilds what was already said from the bus history, silently; it only closes a mouth left open.
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
from app.schemas import (ArgMapSnapshot, BotLine, Claim, Consistency, Control, Event, ModuleStatus, SessionStart,
                         Verdict)

log = logging.getLogger("bots")
CHECK_KIND = {"likely_false": "challenge", "misleading": "challenge", "surprising_true": "concede"}
DISAGREES = {"likely_false": {"supported"},                     # check assessment -> the Verdict labels it would
             "misleading": {"supported", "contradicted"},         # contradict ("true, but" grants the figure)
             "surprising_true": {"contradicted", "missing_context"}}
RECORD_KIND = {"self_contradiction": "uturn", "concession": "conceded"}   # Consistency kind -> line kind
TICK_S = 0.25
BANTER_TTL_S = 10.0       # a banter reply that can't post within this is stale
BANTER_SOON_S = 3.0       # banter only goes to a bot that is free within this and has nothing queued
BANTER_FRESH_S = 8.0      # banter answers a line at most this old (it waits for the bots to be quiet)


class Bots:
    """All the side bots' state. handle() takes bus events, tick() publishes what the pacer releases; per-claim work
    runs as tasks. `clock` is the pacer's clock (time.time; tests pass a fake one)."""

    def __init__(self, publish: Callable[[Event], Any], writer: Optional[Writer] = None,
                 clock: Callable[[], float] = time.time):
        self.publish, self.writer, self.clock = publish, writer or Writer(), clock
        self.pacer = Pacer(config.BOTS_MIN_GAP_S, config.BOTS_LINE_TTL_S, BANTER_TTL_S)
        self.enabled = truthy(config.FLAGS.get("bots", True))
        self.muted = truthy(config.FLAGS.get("mute", False))
        self.tasks: set[asyncio.Task] = set()
        self.reset()

    @property
    def paused(self) -> bool:
        return not self.enabled or self.muted or self.final

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
        self.last_banter = self.banter_posted = -1e18   # the gap starts when a banter starts; given back if none posts
        self.banter_busy = False
        self.pending: set[str] = set()                # claims being answered (check, Verdict wait, line writing)
        self.cue: Optional[tuple[BotLine, float]] = None   # the latest real line banter may answer, and when
        self.findings: set[str] = set()               # Consistency ids already seen: one line each, at most
        self.final = False                            # the final screen is up: paused (the operator's flags kept)
        self.pacer.paused = self.paused

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
            self.verdicts[ev.claim_id] = ev             # a later one (Evidence's deep check) is the better one
            if (w := self.waiting.get(ev.claim_id)) is not None:
                w.set()
        elif name == "Claim":
            self.on_claim(ev)
        elif name == "ArgMapSnapshot":
            self.on_snapshot(ev)
        elif name == "Consistency":
            self.on_consistency(ev)
        elif name == "BotLine":
            self.on_botline(ev)

    def on_control(self, ev: Control) -> None:
        if ev.flag == "bots":
            self.enabled = truthy(ev.value)
        elif ev.flag == "mute":
            self.muted = truthy(ev.value)
        elif ev.flag == "screen":
            self.final = final_screen(ev.value)
        else:
            return
        if self.paused and not self.pacer.paused:
            self.cancel()
            self.banter_busy = False                  # a banter task cancelled before it started never resets it
            self.pending.clear()                      # nor does an answer task
            self.cue = None
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
        self.pending.add(c.id)
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
        finally:
            if session == self.session:
                self.pending.discard(c.id)
                self.maybe_banter()                   # it may have been waiting for this claim

    async def _answer(self, c: Claim, persona: str, session: int) -> None:
        checkable = c.checkable and c.type not in lines.NO_CHECK
        important = c.importance >= lines.IMPORTANT
        if not checkable and not important:
            return
        check = None
        if checkable and important and self.writer.checker is not None:
            check = self.spawn(self.writer.check(c, self.motion))
        verdict = await self.wait_verdict(c.id, config.BOTS_VERDICT_WAIT_S) if checkable else None
        to = dict(persona=persona, target=c.speaker, claim_id=c.id)
        if self.rebutted(c, verdict, check, session, to):
            return
        if check is not None and not lines.supported(verdict):
            res = await check
            verdict = self.verdicts.get(c.id)             # the check can outlast the wait: a later Verdict still wins
            if self.rebutted(c, verdict, check, session, to):
                return
            if not lines.supported(verdict):
                kind, text, why = self.knowledge(c, res, verdict)
                fired = bool(text) and self.queue(session, text=text, kind=kind, **to)
                log_check(c.id, res, f"fired {kind}" if fired else
                          f"not fired: {why or 'not queued (already said, or paused)'}")
                if fired:
                    return
        if check is not None and lines.supported(verdict):
            check.cancel()
            log_check(c.id, finished(check), "not fired: supported verdict")
        granted = lines.supported(verdict)
        if not important:
            return
        raw = await self.writer.followup(c, self.motion, persona, granted, tuple(self.said))
        text = lines.guard(raw, "followup", c.text, strict=True, names=self.names) if raw else None
        if raw and not text:
            log.info("bots: follow-up dropped by the guards: %r", raw)
        if not text or self.pacer.has_said(text):
            text = lines.followup(c, self.said, granted)
        if text:
            self.queue(session, text=text, kind="followup", **to)

    def rebutted(self, c: Claim, verdict: Optional[Verdict], check: Optional[asyncio.Task], session: int,
                 to: dict) -> bool:
        """Queue the rebut a sourced Verdict allows; it beats Claude's check (cancelled, logged). True if it did."""
        text = lines.rebut(c, verdict, self.said) if verdict is not None else None
        if not text:
            return False
        if check is not None:
            check.cancel()
            log_check(c.id, finished(check), "not fired: sourced verdict")
        self.queue(session, text=text, kind="rebut", sourced=True, evidence=list(verdict.evidence), **to)
        return True

    def knowledge(self, c: Claim, res: Any, verdict: Optional[Verdict]) -> tuple[str, Optional[str], str]:
        """The fact line Claude's own-knowledge check `res` allows for `c`: (kind, text or None, why not)."""
        if res is None:
            return "", None, "no answer (timeout or error)"
        kind = CHECK_KIND.get(res.assessment)
        if kind is None:
            return "", None, res.assessment
        if res.confidence < bar(res.assessment)[1]:
            return kind, None, "below the bar"
        if verdict is not None and verdict.label in DISAGREES[res.assessment]:
            return kind, None, f"the Verdict says {verdict.label}"
        if res.assessment == "likely_false":
            text = lines.challenge(c, res.line, res.correction, names=self.names)
        elif res.assessment == "misleading":
            text = lines.misleading(c, res.line, res.context, names=self.names)
        else:
            text = lines.concede(c, res.line, names=self.names, used=self.said)
        return kind, text, "" if text else "dropped by the guards"

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
        for nr in final_nr(snap):
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

    # ------------------------------------------------------------------------------------------- the debate record
    def on_consistency(self, k: Consistency) -> None:
        """A U-turn or a concession from the brain's consistency checker: the bot opposing the side that made it
        heckles that side's speaker, once per finding (see the module doc)."""
        if k.id in self.findings:
            return
        self.findings.add(k.id)                       # seen while paused too: never posted late
        kind = RECORD_KIND.get(k.kind)
        if kind is None or self.paused:
            return
        claim, earlier = self.claims.get(k.claim_id), self.claims.get(k.earlier_claim_id)
        side = str(k.side or getattr(claim, "side", "") or "").lower()
        before = str(k.earlier_side or getattr(earlier, "side", "") or "").lower()
        persona = lines.answering(side)
        if persona is None or (before in lines.SECOND and (before == side) != (kind == "uturn")):
            return                                    # a U-turn is your own; a concession is to the other side
        if kind == "uturn":
            text = lines.uturn(k.then, k.now, self.names, self.said)
        else:
            text = lines.conceded(k.topic or getattr(earlier, "topic", ""),
                                  f"{k.then} {k.now} {getattr(earlier, 'text', '')}", self.names, self.said)
        target = k.speaker or getattr(claim, "speaker", None)
        fired = bool(text) and self.queue(self.session, persona=persona, text=text, kind=kind, target=target,
                                          claim_id=k.claim_id)
        log.info("bots: %s %s by %s -> %s", k.kind, k.id, side, f"{kind} queued" if fired else
                 "not queued" if text else "dropped by the guards")

    # ------------------------------------------------------------------------------------------- banter
    def idle(self, persona: str, within: float = 0.0) -> bool:
        """Free to post within `within` seconds, with nothing of its own waiting (banter never displaces a line)."""
        return self.pacer.free(persona, self.clock() + within) and not self.pacer.queued(persona)

    def quiet(self) -> bool:
        """Nothing important running or waiting for either bot: no claim being answered, no real line queued."""
        return not self.pending and all(ln.kind == "banter" for p in lines.PERSONAS for ln in self.pacer.queued(p))

    def answering_now(self) -> set[str]:
        """The bots with a claim being answered."""
        return {lines.answering(self.claims[cid].side) for cid in self.pending if cid in self.claims}

    def on_botline(self, line: BotLine) -> None:
        if line.src != "bots" or line.kind == "banter" or line.reply_to:
            return                                    # never chained; replayed or foreign lines don't count
        self.cue = (line, self.clock())
        self.maybe_banter()

    def maybe_banter(self) -> None:
        """Start the other bot's banter reply to the cued line if everything allows it now (see the module doc)."""
        if self.cue is None:
            return
        line, at = self.cue
        if self.clock() - at > BANTER_FRESH_S:
            self.cue = None
            return
        if not config.BOTS_BANTER or self.paused or self.writer.fast is None or self.banter_busy or not self.quiet():
            return
        other = lines.OTHER.get(line.persona)
        if not other or not self.idle(other, BANTER_SOON_S) or \
                self.clock() - self.last_banter < config.BOTS_BANTER_GAP_S:
            return
        self.cue = None
        self.last_banter, self.banter_busy = self.clock(), True     # reserves the gap
        self.spawn(self.banter(line, at, other, self.session))

    async def banter(self, line: BotLine, at: float, persona: str, session: int) -> None:
        ok = late = False
        try:
            raw = await self.writer.banter(line.text, persona, line.persona, tuple(self.banters))
            text = lines.guard(raw, "banter", line.text, strict=True, names=self.names) if raw else None
            if raw and not text:
                log.info("bots: banter dropped by the guards: %r", raw)
            late = bool(text) and not (self.quiet() and self.idle(persona, BANTER_SOON_S))   # something came up
            ok = bool(text) and not late and \
                self.queue(session, persona=persona, text=text, kind="banter", reply_to=line.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bots: banter failed")
        finally:
            if session == self.session:
                self.banter_busy = False
                if not ok:
                    self.last_banter = self.banter_posted   # nothing queued: the gap doesn't start yet
                if late and self.cue is None:
                    self.cue = (line, at)             # try again once quiet, while the line is still fresh

    # ------------------------------------------------------------------------------------------- output
    def tick(self) -> None:
        for persona in self.answering_now():
            self.pacer.drop_banter(persona)           # a real line may be on its way: banter never pushes it back
        for ev in self.pacer.tick(self.clock()):
            if isinstance(ev, BotLine):
                self.said.append(ev.text)
                if ev.kind == "banter":
                    self.last_banter = self.banter_posted = self.clock()
                    self.banters.append(ev.text)
            self.publish(ev)
        if not self.banter_busy and not any(ln.kind == "banter" for p in lines.PERSONAS for ln in self.pacer.queued(p)):
            self.last_banter = self.banter_posted     # a queued banter dropped or expired: it gives the gap back
        self.maybe_banter()

    def restore(self, history: list[Event]) -> None:
        """After a crash-restart: the session, claims, verdicts, map statuses, findings, the final screen and lines
        already posted, silently. A line whose BotTalk(on=false) the crash cut off still gets it, at its own
        ts + hold_s (or at the next tick)."""
        open_lines: dict[str, BotLine] = {}
        for ev in history:
            try:
                if ev.event == "SessionStart":
                    self.reset(ev)
                    open_lines.clear()                # the face bridge closes every mouth on SessionStart
                elif ev.event == "Claim":
                    self.remember(ev)
                elif ev.event == "Verdict":
                    self.verdicts[ev.claim_id] = ev
                elif ev.event == "ArgMapSnapshot" and ev.version > self.map_version:
                    self.map_version, self.statuses = ev.version, {n.claim_id: n.status for n in ev.nodes}
                    self.nudged.update(c for nr in final_nr(ev) for c in nr.get("ignored") or [])
                elif ev.event == "BotLine" and ev.src == "bots":
                    self.pacer.posted(ev, ev.ts)
                    self.said.append(ev.text)
                    open_lines[ev.id] = ev
                    if ev.kind == "nudge" and ev.claim_id:
                        self.nudged.add(ev.claim_id)
                    if ev.kind == "banter":
                        self.last_banter = self.banter_posted = ev.ts
                        self.banters.append(ev.text)
                elif ev.event == "BotTalk" and ev.src == "bots" and not ev.on:
                    open_lines.pop(ev.line_id, None)
                elif ev.event == "Consistency":
                    self.findings.add(ev.id)              # answered (or not) before the crash
                elif ev.event == "Control" and ev.flag == "screen":
                    self.final = final_screen(ev.value)
            except Exception:
                log.exception("bots: restore skipped %s", ev.event)
        self.pacer.paused = self.paused
        for line in open_lines.values():
            self.pacer.close_at(line, line.ts + line.hold_s)


def bar(assessment: str) -> tuple[str, float]:
    """The confidence an assessment needs to fire, and its name for the log: a "True, but" (misleading) has its own
    bar, as the check model rates a true but misrepresented figure lower than a plain error."""
    if assessment == "misleading":
        return "misleading bar", config.BOTS_MISLEADING_MIN_CONF
    return "check bar", config.BOTS_CHECK_MIN_CONF


def log_check(cid: str, res: Any, outcome: str) -> None:
    """One INFO line per own-knowledge check, to tune BOTS_CHECK_MIN_CONF and BOTS_MISLEADING_MIN_CONF at
    rehearsal: claim, assessment, confidence, the bar that applied (for an assessment that can fire), and whether a
    line fired (or why not)."""
    if res is None:
        log.info("bots: check %s: no result -> %s", cid, outcome)
    elif res.assessment in CHECK_KIND:
        name, at = bar(res.assessment)
        log.info("bots: check %s: %s %.2f (%s %.2f) -> %s", cid, res.assessment, res.confidence, name, at, outcome)
    else:
        log.info("bots: check %s: %s %.2f -> %s", cid, res.assessment, res.confidence, outcome)


def finished(task: asyncio.Task) -> Any:
    """The task's result if it already finished cleanly, else None."""
    return task.result() if task.done() and not task.cancelled() and task.exception() is None else None


def final_screen(value: Any) -> bool:
    """Control(flag="screen"): "final" is the verdict screen; any other value ("live") is back to the debate."""
    return str(value or "").strip().lower() == "final"


def final_nr(snap: ArgMapSnapshot) -> list[dict]:
    """The snapshot's non-responsive turns that are final: a turn still open (final=False, flagged at a Pause) may
    yet answer the point. An entry without the field counts as final."""
    return [nr for nr in snap.nonresponsive or [] if nr.get("final", True)]


async def run(bus) -> None:
    from app.bots.writer import make_writer

    # Subscribe and snapshot the history in one synchronous step: every event is restored or handled live, once.
    sub = bus.subscribe("bots", {"SessionStart", "Claim", "Verdict", "ArgMapSnapshot", "Consistency", "Control",
                                 "BotLine"}, maxsize=512)
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
