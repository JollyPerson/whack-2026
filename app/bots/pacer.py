"""Side-bot pacing. Owner: B. Pure: every call takes `now`, so tests drive the clock.

One queue per bot. A line posts the moment it is ready unless its bot posted less than a gap ago, where the gap is
max(min_gap, the previous bubble's hold_s): a bubble is never replaced while it is still up. When a bot is free
again it posts its best queued line (priority rebut > uturn > challenge > concede > nudge > conceded > followup >
banter, then the newest).
- A queued line expires after `ttl` seconds (banter after `banter_ttl`: a late reply makes no sense).
- Banter is lowest of all: a bot's queued banter is dropped when a real line for that bot is pushed (or by
  drop_banter, when one may be on its way), and banter is refused while one is queued.
- No text twice in a session, and at most one fact line (rebut, challenge or concede) per claim.
- Every posted BotLine gets its BotTalk(on=false) hold_s later, from tick().
- paused (bots=false, mute): push() drops everything; it survives reset(), like the operator's switches.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.bots.lines import FACT_KINDS, PERSONAS, PRIORITY, text_key
from app.schemas import BotLine, BotTalk, Event


@dataclass
class Queued:
    line: BotLine
    at: float
    expires: float

    @property
    def rank(self) -> tuple:
        return PRIORITY.get(self.line.kind, 0), self.at


class Pacer:
    def __init__(self, min_gap: float = 10.0, ttl: float = 30.0, banter_ttl: float = 10.0, max_queue: int = 8):
        self.min_gap, self.ttl, self.banter_ttl, self.max_queue = min_gap, ttl, banter_ttl, max_queue
        self.paused = False
        self.reset()

    def reset(self) -> None:
        self.queues: dict[str, list[Queued]] = {p: [] for p in PERSONAS}
        self.last = dict.fromkeys(PERSONAS, -math.inf)
        self.last_hold = dict.fromkeys(PERSONAS, 0.0)
        self.texts: set[str] = set()
        self.fact_claims: set[str] = set()
        self.offs: list[tuple[float, BotTalk]] = []

    def has_said(self, text: str) -> bool:
        return text_key(text) in self.texts

    def taken(self, line: BotLine) -> bool:
        """Already said this session: the same text, or a second fact line about the same claim."""
        return self.has_said(line.text) or (line.kind in FACT_KINDS and line.claim_id in self.fact_claims)

    def push(self, line: BotLine, now: float) -> bool:
        """Queue a line; False if it was dropped at once."""
        if self.paused or line.persona not in self.queues or self.taken(line):
            return False
        q = self.queues[line.persona]
        if line.kind == "banter" and any(x.line.kind != "banter" for x in q):
            return False                              # banter never waits behind a real line
        if line.kind != "banter":
            self.drop_banter(line.persona)            # a real line ready first: the banter goes
        ttl = min(self.banter_ttl, self.ttl) if line.kind == "banter" else self.ttl
        q.append(Queued(line, now, now + ttl))
        if len(q) > self.max_queue:
            q.remove(min(q, key=lambda x: x.rank))
        return True

    def drop_banter(self, persona: str) -> None:
        q = self.queues.get(persona, [])
        q[:] = [x for x in q if x.line.kind != "banter"]

    def free(self, persona: str, now: float) -> bool:
        return now - self.last[persona] >= max(self.min_gap, self.last_hold[persona])

    def tick(self, now: float) -> list[Event]:
        """What to publish now, in order: due BotTalk(on=false), then at most one BotLine per bot."""
        out: list[Event] = [talk for t, talk in self.offs if t <= now]
        self.offs = [(t, talk) for t, talk in self.offs if t > now]
        for p in PERSONAS:
            q = self.queues[p] = [x for x in self.queues[p] if x.expires > now and not self.taken(x.line)]
            if q and self.free(p, now):
                best = max(q, key=lambda x: x.rank)
                q.remove(best)
                self.posted(best.line, now)
                self.close_at(best.line, now + best.line.hold_s)
                out.append(best.line)
        return out

    def close_at(self, line: BotLine, t: float) -> None:
        """Schedule `line`'s BotTalk(on=false) for `t` (tick() publishes it once due)."""
        self.offs.append((t, BotTalk(persona=line.persona, line_id=line.id, on=False)))

    def posted(self, line: BotLine, now: float) -> None:
        """Count a line as said (tick() does this; a crash-restart replays the session's lines through it)."""
        if line.persona in self.last:
            self.last[line.persona], self.last_hold[line.persona] = now, line.hold_s
        self.texts.add(text_key(line.text))
        if line.kind in FACT_KINDS and line.claim_id:
            self.fact_claims.add(line.claim_id)

    def clear(self) -> list[BotTalk]:
        """Drop every queued line; returns the pending BotTalk(on=false), for the caller to publish now."""
        offs = [talk for _, talk in self.offs]
        self.offs = []
        for q in self.queues.values():
            q.clear()
        return offs

    def queued(self, persona: str) -> list[BotLine]:
        return [x.line for x in self.queues.get(persona, [])]
