"""In-process pub/sub + JSONL log + replayer. Owner: B.

Single asyncio event loop => publish() is synchronous and never blocks.
- Topics are event names ("Claim", "Verdict", ...). subscribe(name, {...}) — empty set = everything.
- One bounded queue per subscriber; on overflow the OLDEST event is dropped (fresh beats complete).
- Every event is written to the JSONL log BEFORE fan-out, so the log never loses anything.
- Called from another thread (e.g. an STT SDK callback)? Use publish_threadsafe().
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from app.schemas import Event, SessionStart, parse_event

log = logging.getLogger("bus")

# History kept for the websocket Snapshot: latest revision per key for these event types.
COMPACT = {"Utterance": "id", "ArgMapSnapshot": None, "ResearchProgress": None, "ModuleStatus": "module",
           "Claim": "id", "Verdict": "claim_id", "DirectorStatus": None}
HISTORY_SKIP: set[str] = set()            # event types kept in the log only, not in the ws snapshot
SURVIVES_SESSION = {"ModuleStatus"}       # still true after a new SessionStart
# Never trimmed by the history cap: motion, sides, facts, module dots, and the singletons (one row each, compacted
# into the slot of their FIRST publish, so they would otherwise be the first latest-state rows to go).
PINNED = SURVIVES_SESSION | {"SessionStart", "FactSheet"} | {name for name, f in COMPACT.items() if f is None}
HISTORY_MAX = 5000                        # over the cap: drop the oldest unpinned rows down to 80 % in one go


def _key(ev: Event):
    f = COMPACT[ev.event]
    return ev.event, getattr(ev, f, None) if f else None


# Who opened a Sub: main.supervise() sets a fresh token per module attempt, and tasks the module spawns copy it,
# so a crashed attempt's subscriptions can be closed before the restart (else they fill up as zombies).
SUB_OWNER: ContextVar[Optional[object]] = ContextVar("bus_sub_owner", default=None)


def age(ev: Event) -> float:
    """Seconds since the event was published (the replayer rebases ts, so this works in replay too)."""
    return time.time() - ev.ts


class Sub:
    def __init__(self, bus: "Bus", name: str, events: set[str], where, maxsize: int):
        self.bus, self.name, self.events, self.where = bus, name, events, where
        self.q: asyncio.Queue = asyncio.Queue(maxsize)
        self.dropped = 0
        self.owner = SUB_OWNER.get()

    def offer(self, ev: Event) -> None:
        if self.events and ev.event not in self.events:
            return
        try:
            if self.where and not self.where(ev):
                return
        except Exception:                        # a broken filter must not break publish()
            log.exception("filter of %s failed", self.name)
            return
        if self.q.full():
            self.q.get_nowait()
            self.dropped += 1
            if self.dropped in (1, 10, 100, 1000):
                log.warning("subscriber %s dropped %d events (too slow)", self.name, self.dropped)
        self.q.put_nowait(ev)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        return await self.q.get()

    def close(self) -> None:
        if self in self.bus.subs:
            self.bus.subs.remove(self)


class Bus:
    def __init__(self, log_path: Optional[Path] = None):
        self.subs: list[Sub] = []
        self._seq = itertools.count(1)
        self.last_seq = 0
        self.history: list[Event] = []
        self._slot: dict[tuple, int] = {}
        self.log_path = log_path
        self._log = open(log_path, "a", buffering=1) if log_path else None   # line-buffered
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._warned: set[tuple[str, str]] = set()

    def subscribe(self, name: str, events: Iterable[str] = (), where: Optional[Callable[[Event], bool]] = None,
                  maxsize: int = 256) -> Sub:
        s = Sub(self, name, set(events), where, maxsize)
        self.subs.append(s)
        return s

    def close_owned(self, owner: object) -> int:
        """Close every Sub opened under SUB_OWNER == owner; returns how many."""
        mine = [s for s in self.subs if owner is not None and s.owner is owner]
        for s in mine:
            s.close()
        return len(mine)

    def publish(self, ev: Union[Event, dict, str, bytes], src: str = "") -> Event:
        if not isinstance(ev, Event):
            ev = parse_event(ev)
        ev.seq = self.last_seq = next(self._seq)
        if not ev.ts:
            ev.ts = time.time()
        if src and not ev.src:
            ev.src = src
        if ev.model_extra:                           # flexible, but drift stays visible: warn once per field
            for f in ev.model_extra:
                if (ev.event, f) not in self._warned:
                    self._warned.add((ev.event, f))
                    log.info("note: %s carries extra field %r (from %s) - fine, but tell B if it should be official",
                             ev.event, f, ev.src or "?")
        if self._log:
            self._log.write(ev.model_dump_json() + "\n")
        self._remember(ev)
        for s in list(self.subs):
            s.offer(ev)
        return ev

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish_threadsafe(self, ev: Union[Event, dict], src: str = "") -> None:
        """For callbacks running on another thread (many STT/TTS SDKs do this)."""
        loop = self._loop or asyncio.get_event_loop()
        loop.call_soon_threadsafe(self.publish, ev, src)

    def _remember(self, ev: Event) -> None:
        if isinstance(ev, SessionStart):
            self._reindex([e for e in self.history if e.event in SURVIVES_SESSION])
        if ev.event in HISTORY_SKIP:
            return
        if ev.event in COMPACT:
            key = _key(ev)
            i = self._slot.get(key)
            if i is not None:
                old = self.history[i]
                if ev.event == "Utterance" and getattr(old, "final", False) and not getattr(ev, "final", True):
                    return                           # a late interim never overwrites its final
                self.history[i] = ev
                return
            self._slot[key] = len(self.history)
        self.history.append(ev)
        if len(self.history) > HISTORY_MAX:          # keep the snapshot small on very long runs
            drop = len(self.history) - int(HISTORY_MAX * 0.8)
            keep = []
            for e in self.history:                   # oldest first; order is preserved
                if drop > 0 and e.event not in PINNED:
                    drop -= 1
                else:
                    keep.append(e)
            self._reindex(keep)

    def _reindex(self, events: list[Event]) -> None:
        self.history = events
        self._slot = {_key(e): i for i, e in enumerate(events) if e.event in COMPACT}

    def close(self) -> None:
        if self._log:
            self._log.close()
            self._log = None


# ----------------------------------------------------------------------------- replayer
TIME_FIELDS = ("ts", "t", "t_start", "t_end")
MAX_GAP_S = 5.0              # replayed silences longer than this are cut to this (setup time, idle minutes)
MAX_SPEECH_S = 120.0         # an own start time further before ts than this is another clock: ignored
REPLAY_SKIP = {"ModuleStatus"}   # full replay: module health is about THIS process, not the recorded one


def load_jsonl(path: Path) -> list[Event]:
    out = []
    for i, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            out.append(parse_event(line))
        except Exception as e:                      # one bad line must not kill a demo replay
            log.warning("%s:%d skipped (%s)", path, i, e)
    return out


def last_session(events: list[Event]) -> list[Event]:
    """Only the last session that has speech in it (rehearsals earlier in the same log are dropped, and so is a
    trailing New debate click nobody spoke after). No speech in any session: from the last SessionStart on."""
    starts = [i for i, e in enumerate(events) if e.event == "SessionStart"]
    if not starts:
        return events
    said = [i for i, e in enumerate(events) if e.event == "Utterance"]
    first = ([i for i in starts if said and i < said[-1]] or starts)[-1]
    return events[first:next((i for i in starts if i > first), len(events))]


def _begin(ev: Event) -> float:
    """When the event's own content started: a final Utterance arrives at the END of its speech, so its t_start
    is earlier than ts. Everything else (and 0 = unknown, or a t_start on another clock): ts."""
    v = getattr(ev, "t_start", None)
    return v if isinstance(v, (int, float)) and v and ev.ts - MAX_SPEECH_S <= v < ev.ts else ev.ts


def gap_offsets(times: list[float], max_gap: float, begins: Optional[list[float]] = None) -> list[float]:
    """Per (sorted) time: seconds of idle cut before it, so no gap exceeds max_gap (0 = keep every gap).
    begins[i] <= times[i] is when event i's own content started: its gap runs from the previous time to there,
    so the speech before a final is never cut as if it were silence."""
    out, off = [], 0.0
    for i, t in enumerate(times):
        gap = (begins[i] if begins else t) - times[i - 1] if i else 0.0
        if max_gap and gap > max_gap:
            off += gap - max_gap
        out.append(off)
    return out


def rebase(ev: Event, shift: Callable[[float], float]) -> Event:
    for f in TIME_FIELDS:
        v = getattr(ev, f, None)
        if isinstance(v, (int, float)) and v:
            setattr(ev, f, shift(float(v)))
    marks = getattr(ev, "marks", None)
    if marks:
        ev.marks = {k: shift(v) for k, v in marks.items()}
    return ev


async def replay(bus: Bus, path: Path, speed: float = 1.0, only: Iterable[str] = (),
                 src: str = "replay", loop: bool = False, last: bool = True, max_gap: float = MAX_GAP_S) -> int:
    """Re-emit recorded events with their original spacing (or Nx faster); returns how many were sent.
    only={"Utterance","Pause"} re-drives the live pipeline from recorded ears; empty `only` = full
    replay (demo backup, no APIs needed) minus REPLAY_SKIP. last=True starts at the last SessionStart;
    silences > max_gap s are cut to max_gap. Files may use relative seconds (ts=0.0 at start)."""
    only, n = set(only), 0
    while True:
        evs = sorted(load_jsonl(path), key=lambda e: e.ts)       # FileNotFoundError is the caller's to report
        evs = [e for e in (last_session(evs) if last else evs)
               if (e.event in only if only else e.event not in REPLAY_SKIP)]
        if not evs:
            log.warning("replay: nothing to replay in %s", path)
            return n
        t0, w0 = evs[0].ts, time.time()
        for ev, off in zip(evs, gap_offsets([e.ts for e in evs], max_gap, [_begin(e) for e in evs])):
            def shift(t: float, off: float = off) -> float:
                return w0 + (t - t0 - off) / speed

            d = shift(ev.ts) - time.time()
            if d > 0:
                await asyncio.sleep(d)
            ev = rebase(ev.model_copy(), shift)
            ev.src = f"{src}:{ev.src}" if ev.src else src
            bus.publish(ev)
            n += 1
        if not loop:
            return n
