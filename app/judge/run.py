"""The judge, wired onto the bus. Owner: B. Runs only with JUDGE=real (no profile starts it).

Consumes: SessionStart, Control (flag screen, value final), and the session's record: Utterance (final), Claim,
          Verdict, BotLine, Interjection, ArgMapSnapshot
Produces: DebateResult (src="judge"), ModuleStatus(judge); writes {JUDGE_DIR}/<run>-summary.md and .json

When the operator shows the final screen, the judge rates the CURRENT session: the bus history since the last
SessionStart, merged with what this module saw itself (in case the history cap trimmed rows). scorecard.py computes
the fixed scorecard, writer.py has Claude weigh it with the transcript (or the formula decides), report.py writes the
summary, and one DebateResult goes out: winner, margin, headline, scores, the report path, a dance per side bot
(dances_for: the winner's bot JUDGE_DANCE_WIN, the loser's JUDGE_DANCE_LOSE, both JUDGE_DANCE_DRAW on a draw; the For
bot is Prop's), music per JUDGE_MUSIC (music_for: winner = only the winner's bot, so a draw is silent; none; both)
and dance_s = JUDGE_DANCE_S. The face turns it into the bots' dances.
Once per session: a later final click judges again only if new final Utterances arrived since the last judgement,
otherwise it publishes nothing. The judgement runs as a background task (the bus loop never waits on Claude); a click
while one runs is ignored. SessionStart resets everything and cancels a judgement in flight. After a crash-restart,
a DebateResult already in the history counts as judged.
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


class Judge:
    """The judge's state for one session. handle() takes bus events; a final-screen click spawns judge()."""

    def __init__(self, publish: Callable[[Event], Any], llm: Any = None,
                 history: Callable[[], list[Event]] = list, log_path: Optional[Path] = None):
        self.publish, self.llm, self.history = publish, llm, history
        # <run> in <run>-summary.md: the bus log's stem, else when this module started (one name per run)
        self.run_name = Path(log_path).stem if log_path else time.strftime("%Y%m%d-%H%M%S")
        self.task: Optional[asyncio.Task] = None
        self.session = 0
        self.reset()

    def reset(self, ev: Optional[SessionStart] = None) -> None:
        self.cancel()
        self.session += 1
        self.seen: list[Event] = [ev] if ev is not None else []   # this session's record as this module saw it
        self.snapshot: Optional[ArgMapSnapshot] = None           # only the newest (they come several times a second)
        self.judged: Optional[frozenset] = None                  # final Utterance ids at the last judgement
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
            self.on_final()
        elif name == "ArgMapSnapshot":
            if self.snapshot is None or ev.version >= self.snapshot.version:
                self.snapshot = ev
        elif name in RECORD and (name != "Utterance" or ev.final):
            self.seen.append(ev)

    def events(self) -> list[Event]:
        """The session's events in publish order: the bus history plus what this module saw, each once."""
        mine = self.seen + ([self.snapshot] if self.snapshot is not None else [])
        merged: dict[Any, Event] = {}
        for e in [*self.history(), *mine]:
            merged.setdefault(e.seq or id(e), e)
        return sorted(merged.values(), key=lambda e: e.seq)

    def on_final(self) -> None:
        if self.task is not None and not self.task.done():
            log.info("judge: still judging; final screen click ignored")
            return
        rec = scorecard.collect(self.events())
        speech = frozenset(u.id for u in rec.utterances)
        if not speech:
            self.status("up", "nothing to judge: no speech this session")
            return
        if self.judged is not None and not speech - self.judged:
            log.info("judge: this session is judged and nobody spoke since; nothing re-published")
            return
        self.task = asyncio.create_task(self.judge(rec, speech, self.session))

    # ------------------------------------------------------------------------------------------- judging
    async def judge(self, rec: scorecard.Record, speech: frozenset, session: int) -> None:
        try:
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
            self.judged = speech
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
        """After a crash-restart: this session's judgement, if there was one (its speech and its summary file)."""
        speech: set[str] = set()
        for ev in history:
            if ev.event == "SessionStart":
                self.reset(ev)
                speech = set()
            elif ev.event == "Utterance" and ev.final and ev.text.strip():
                speech.add(ev.id)
            elif ev.event == "DebateResult" and ev.src == "judge":
                self.judged = frozenset(speech)
                if ev.report:
                    p = Path(ev.report)
                    self.path = p if p.is_absolute() else report.ROOT / p


async def run(bus) -> None:
    # Subscribe and snapshot the history in one synchronous step (as app/bots does).
    sub = bus.subscribe("judge", {"SessionStart", "Control", *RECORD},
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
