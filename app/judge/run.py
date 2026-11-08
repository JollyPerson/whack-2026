"""The judge, wired onto the bus. Owner: B. Runs only with JUDGE=real (no profile starts it).

Consumes: SessionStart, Control (flag screen, value final), DebateResult (from another src), and the session's
          record: Utterance (final), Claim, Verdict, BotLine, Interjection, ArgMapSnapshot, Consistency, and the
          brain's DebateSummary
Produces: DebateResult (src="judge"), ModuleStatus(judge); writes {JUDGE_DIR}/<run>-summary.md and .json

When the operator shows the final screen, the judge rates the CURRENT session: the bus history since the last
SessionStart, merged with what this module saw itself (in case the history cap trimmed rows). It first lets the
record settle, for SETTLE_S at most: until no checkable claim from the last RECENT_S is still without a Verdict and
nothing new (speech, claim, verdict, interjection, argument map) arrived for QUIET_S. A click right after the closing
speech is the natural demo flow, and its claims, verdicts and the final argument map trail it by 1-6 s. Otherwise it
judges at once. The brain makes its DebateSummary on the same click (a few seconds: it first checks the last turn's
consistency), so the judge also waits, until JUDGE_SUMMARY_WAIT_S after the click at most, for the session's summary
to be current: published after the click, or made from the argument map the record ends on (one already in the
history counts). Without one by then (the brain is off or failed, or it is late) it judges with the newest older one
of the session if there is one, else without, and the report says so (Record.summary_note). scorecard.py computes
the fixed scorecard, writer.py has Claude weigh it with the transcript (or the formula decides), report.py writes the
summary, and one DebateResult goes out: winner, margin, headline, scores, the report path, a dance per side bot
(dances_for: the winner's bot JUDGE_DANCE_WIN, the loser's JUDGE_DANCE_LOSE, both JUDGE_DANCE_DRAW on a draw; the For
bot is Prop's), music per JUDGE_MUSIC (music_for: winner = only the winner's bot, so a draw is silent; none; both)
and dance_s = JUDGE_DANCE_S. The face turns it into the bots' dances.
Once per session: a later final click judges again only if new final Utterances or new (or revised) Verdicts
arrived since the last judgement, otherwise it publishes nothing. The judgement runs as a background task (the bus
loop never waits on Claude); a click while one runs is ignored. SessionStart resets everything and cancels a
judgement in flight. After a crash-restart, a DebateResult already in the history counts as judged.
Replays: a final click replayed from a log (src "replay:...") is ignored, only a live click judges; a DebateResult
from another src (a replayed one) counts as this session's judgement and cancels one in flight, so a replayed log
never shows two verdicts.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app import config
from app.judge import report, scorecard, writer
from app.schemas import ArgMapSnapshot, DebateResult, Event, ModuleStatus, SessionStart

log = logging.getLogger("judge")
RECORD = {"Utterance", "Claim", "Verdict", "BotLine", "Interjection", "ArgMapSnapshot"}
FINDINGS = {"Consistency", "DebateSummary"}   # the brain's consistency findings and summary: kept, never "growth"
SETTLE_S = 8.0            # at a final click, wait at most this long for the record to settle
RECENT_S = 60.0           # ... for verdicts on checkable claims this recent (older ones with no verdict won't get one)
QUIET_S = 3.0             # ... and until nothing new arrived for this long (claims trail speech by ~1 s)
BOT = {"prop": "for", "opp": "against"}       # the side -> the bot that seconds it
PERSONAS = ("for", "against")
MUSIC_MODES = ("winner", "none", "both")
_warned: set[str] = set()


def dances_for(winner: str) -> dict[str, str]:
    """Bot persona -> dance title: the winner's bot JUDGE_DANCE_WIN, the loser's JUDGE_DANCE_LOSE, a draw both
    JUDGE_DANCE_DRAW."""
    if winner not in BOT:
        return {p: config.JUDGE_DANCE_DRAW for p in PERSONAS}
    return {p: config.JUDGE_DANCE_WIN if p == BOT[winner] else config.JUDGE_DANCE_LOSE for p in PERSONAS}


def music_for(winner: str) -> dict[str, bool]:
    """Bot persona -> play its dance's music, per JUDGE_MUSIC: winner (only the winner's bot; a draw is silent),
    none, or both."""
    mode = str(config.JUDGE_MUSIC or "").strip().lower()
    if mode not in MUSIC_MODES:
        if mode not in _warned:
            _warned.add(mode)
            log.warning("JUDGE_MUSIC=%r: use %s; using winner", config.JUDGE_MUSIC, "|".join(MUSIC_MODES))
        mode = "winner"
    return {p: mode == "both" or (mode == "winner" and BOT.get(winner) == p) for p in PERSONAS}


def is_final_screen(ev: Event) -> bool:
    return ev.event == "Control" and ev.flag == "screen" and str(ev.value).strip().lower() == "final"


def replayed(ev: Event) -> bool:
    return str(ev.src or "").startswith("replay")


def progress(rec: scorecard.Record) -> frozenset:
    """What a judgement of `rec` rests on: its final Utterances, its Verdicts as counted, its consistency findings and
    whether it had the brain's summary. A later click re-judges only if this grew or changed (a summary that came too
    late for the first judgement is used by the next click)."""
    return frozenset([("u", u.id) for u in rec.utterances]
                     + [("v", cid, scorecard.counted_label(v)) for cid, v in rec.verdicts.items()]
                     + [("k", k.claim_id, k.earlier_claim_id) for k in rec.consistency]
                     + ([("s",)] if rec.summary is not None else []))


def current(rec: scorecard.Record, after_seq: int) -> bool:
    """The record's DebateSummary is this click's: published after it, or made from the argument map the record ends
    on (nothing changed since). With no argument map to compare, any summary of the session is."""
    s = rec.summary
    return s is not None and (s.seq > after_seq or rec.snapshot is None or s.version >= rec.snapshot.version)


def in_flight(rec: scorecard.Record, now: float) -> set[str]:
    """Checkable claims from the last RECENT_S with no Verdict yet."""
    return {c.id for c in rec.claims.values()
            if scorecard.checkable(c) and c.id not in rec.verdicts and now - (c.ts or now) < RECENT_S}


class Judge:
    """The judge's state for one session. handle() takes bus events; a final-screen click spawns judge()."""

    def __init__(self, publish: Callable[[Event], Any], llm: Any = None,
                 history: Callable[[], list[Event]] = list, log_path: Optional[Path] = None):
        self.publish, self.llm, self.history = publish, llm, history
        # <run> in <run>-summary.md: the bus log's stem, else when this module started (one name per run)
        self.run_name = Path(log_path).stem if log_path else time.strftime("%Y%m%d-%H%M%S")
        self.task: Optional[asyncio.Task] = None
        self.session = 0
        self.arrived = asyncio.Event()                         # set when the record grows (wakes a settle)
        self.summarised = asyncio.Event()                      # set when a DebateSummary arrives
        self.heard = float("-inf")                             # when it last grew (monotonic)
        self.reset()

    def reset(self, ev: Optional[SessionStart] = None) -> None:
        self.cancel()
        self.session += 1
        self.seen: list[Event] = [ev] if ev is not None else []   # this session's record as this module saw it
        self.snapshot: Optional[ArgMapSnapshot] = None           # only the newest (they come several times a second)
        self.judged: Optional[frozenset] = None                  # progress() at the last judgement
        self.path: Optional[Path] = None                         # this session's summary (re-judging rewrites it)

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def status(self, state: str, detail: str) -> None:
        self.publish(ModuleStatus(module="judge", state=state, detail=detail[:200]))

    # ------------------------------------------------------------------------------------------- events
    def handle(self, ev: Event) -> None:
        name = ev.event
        if name == "SessionStart":
            self.reset(ev)
        elif is_final_screen(ev):
            if replayed(ev):
                log.info("judge: a replayed final screen click (%s) ignored; only a live click judges", ev.src)
            else:
                self.on_final(ev.seq)
        elif name == "DebateResult":
            if ev.src != "judge":
                self.on_foreign_result(ev)
        elif name == "ArgMapSnapshot":
            if self.snapshot is None or ev.version >= self.snapshot.version:
                if self.snapshot is None or ev.version > self.snapshot.version:
                    self.grew()
                self.snapshot = ev
        elif name in FINDINGS:
            self.seen.append(ev)
            if name == "DebateSummary":
                self.summarised.set()
        elif name in RECORD and (name != "Utterance" or ev.final):
            self.seen.append(ev)
            if name != "BotLine":                              # the bots may banter on; the record is the rest
                self.grew()

    def grew(self) -> None:
        self.heard = time.monotonic()
        self.arrived.set()

    def events(self) -> list[Event]:
        """The session's events in publish order: the bus history plus what this module saw, each once."""
        mine = self.seen + ([self.snapshot] if self.snapshot is not None else [])
        merged: dict[Any, Event] = {}
        for e in [*self.history(), *mine]:
            merged.setdefault(e.seq or id(e), e)
        return sorted(merged.values(), key=lambda e: e.seq)

    def on_final(self, click: int = 0) -> None:
        if self.task is not None and not self.task.done():
            log.info("judge: still judging; final screen click ignored")
            return
        rec = scorecard.collect(self.events())
        if not rec.utterances:
            self.status("up", "nothing to judge: no speech this session")
            return
        if self.judged is not None and progress(rec) <= self.judged:
            log.info("judge: this session is judged and nothing new was said or checked since; nothing re-published")
            return
        self.task = asyncio.create_task(self.judge(self.session, click))

    def on_foreign_result(self, ev: DebateResult) -> None:
        """A DebateResult from another src (a replayed log's): it is this session's verdict. One verdict only."""
        if self.task is not None and not self.task.done():
            log.info("judge: a DebateResult from %s arrived; the judgement in flight is dropped", ev.src)
        self.cancel()
        self.judged = progress(scorecard.collect(self.events()))

    # ------------------------------------------------------------------------------------------- judging
    async def settle(self) -> scorecard.Record:
        """The session's record once the fact checks in flight have landed and nothing new arrived for QUIET_S, or
        after SETTLE_S at most."""
        end, told = time.monotonic() + SETTLE_S, False
        while True:
            now = time.monotonic()
            rec = scorecard.collect(self.events())
            waiting, quiet_at = in_flight(rec, time.time()), self.heard + QUIET_S
            if now >= end or (not waiting and now >= quiet_at):
                return rec
            if not told:
                told, n = True, len(waiting)
                self.status("up", f"waiting up to {end - now:.0f} s for the record to settle"
                                  + (f": {n} fact check{'s' if n != 1 else ''} in flight" if n else ""))
            self.arrived.clear()
            try:
                await asyncio.wait_for(self.arrived.wait(), (end if waiting else min(end, quiet_at)) - now)
            except TimeoutError:
                pass

    async def await_summary(self, rec: scorecard.Record, click: int, end: float) -> scorecard.Record:
        """The record once its DebateSummary is current (see current()), or at `end` (monotonic) with what there is:
        an older summary of the session, or none. Sets rec.summary_note when it is not current."""
        told = False
        while not current(rec, click):
            now = time.monotonic()
            if now >= end:
                wait = float(config.JUDGE_SUMMARY_WAIT_S)
                s, snap = rec.summary, rec.snapshot
                rec.summary_note = (f"it did not arrive within {wait:g} s of the Final screen click" if s is None else
                                    f"the newest predates the click and the end of the record (argument map version "
                                    f"{s.version} of {snap.version if snap is not None else '?'}): the last points "
                                    f"may be missing")
                log.info("judge: debate summary: %s", rec.summary_note)
                return rec
            if not told:
                told = True
                self.status("up", f"waiting up to {end - now:.0f} s for the brain's debate summary")
            self.summarised.clear()
            try:
                await asyncio.wait_for(self.summarised.wait(), end - now)
            except TimeoutError:
                pass
            rec = scorecard.collect(self.events())
        return rec

    async def judge(self, session: int, click: int = 0) -> None:
        try:
            end = time.monotonic() + max(float(config.JUDGE_SUMMARY_WAIT_S), 0.0)
            rec = await self.settle()
            rec = await self.await_summary(rec, click, end)
            self.status("up", f"judging: {len(rec.utterances)} utterances, {len(rec.claims)} claims")
            card = scorecard.scorecard(rec)
            j = await writer.decide(self.llm, rec, card)
            dances, music, dance_s = dances_for(j.winner), music_for(j.winner), float(config.JUDGE_DANCE_S)
            where = ""
            try:
                self.path = self.path or report.new_path(self.run_name)
                path = await asyncio.to_thread(report.write, self.path, rec, card, j, dances, music, dance_s,
                                               None, self.run_name)
                where = report.display(path)
            except Exception:
                log.exception("judge: writing the summary failed")
            if session != self.session:
                return
            self.publish(DebateResult(winner=j.winner, margin=j.margin, headline=j.headline, scores=j.scores,
                                      report=where, dances=dances, music=music, dance_s=dance_s, via=j.via))
            self.judged = progress(rec)
            verdict = "draw" if j.winner == "draw" else f"{j.winner} wins ({j.margin})"
            log.info("judge: %s via %s: %s -> %s", verdict, j.via, j.headline, where or "no summary file")
            self.status("up" if where else "degraded",
                        f"{verdict} via {j.via}" + (f": {Path(where).name}" if where else ": summary not written"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("judge: judging failed")
            self.status("degraded", f"judging failed: {e!r}")

    def restore(self, history: list[Event]) -> None:
        """After a crash-restart: this session's judgement, if there was one (what it rested on and its summary file;
        a replayed DebateResult counts as judged too, but its file is not ours to rewrite)."""
        session: list[Event] = []
        for ev in history:
            if ev.event == "SessionStart":
                self.reset(ev)
                session = []
            session.append(ev)
            if ev.event == "DebateResult":
                self.judged = progress(scorecard.collect(session))
                if ev.report and ev.src == "judge":
                    p = Path(ev.report)
                    self.path = p if p.is_absolute() else report.ROOT / p


async def run(bus) -> None:
    # Subscribe and snapshot the history in one synchronous step (as app/bots does).
    sub = bus.subscribe("judge", {"SessionStart", "Control", "DebateResult", *RECORD, *FINDINGS},
                        where=lambda e: e.event != "Utterance" or e.final, maxsize=4096)
    history = list(bus.history)
    try:
        llm = await asyncio.to_thread(writer.make_judge_llm)   # SDK import (~1 s) off the shared event loop
    except asyncio.CancelledError:
        sub.close()
        raise
    except Exception:
        log.exception("judge: LLM client unavailable; the scorecard formula decides")
        llm = None
    judge = Judge(lambda ev: bus.publish(ev, src="judge"), llm, history=lambda: bus.history, log_path=bus.log_path)
    judge.restore(history)
    name = getattr(llm, "name", None) or "none (the scorecard formula decides)"
    bus.publish(ModuleStatus(module="judge", state="up", detail=f"llm: {name}; waiting for the final screen"),
                src="judge")
    log.info("judge up: %s", name)
    try:
        async for ev in sub:
            try:
                judge.handle(ev)
            except Exception:
                log.exception("judge failed on %s", ev.event)
    finally:
        judge.cancel()
        sub.close()
