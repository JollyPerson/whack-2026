"""Argument map state. Owner: B.

Pure + deterministic: no clock, no I/O, no randomness. Rebuildable by replaying Utterance/Pause/Claim/Verdict
events in log order (run.py does this after a crash-restart). Every state change bumps `version`, which never
goes down (not even on reset), so "newest version wins" is always safe for consumers.

Code, not the LLM, decides (design §7.3, reconciled):
- side: from SessionStart.speakers; otherwise first speaker seen = prop, then whichever side has fewer speakers.
- turn: maximal run of finals from one speaker; it closes when another speaker's final arrives. A 1-2 word
  final from the other speaker ("No!", "Yeah.") is held: it only opens a turn if that speaker keeps going.
  A claim extracted from a held final never opens or closes a turn: it joins the turn its speaker goes on to
  open, else it stays filed under the turn it interrupted (node.turn, not in that turn's claims).
- status: minor (importance < 0.5) | answered (attacked by the other side) | conceded (only supported by the
  other side) | unanswered (no response link after >= k_turns CLOSED substantive turns by the other side;
  substantive = >= 1 claim or >= min_turn_s long) | open. A late reply moves unanswered -> answered/conceded.
- non-responsive turn T: the latest earlier opposing turn that has claims holds eligible points T's side has not
  already replied to, T has >= 2 claims, and none of T's claims links to that opposing turn. Provisional
  (final=False) at a Pause once T has run >= min_nr_turn_s; final when T closes.
- scoreboard (§7.5): score = (S + 0.5M + 1) / (S + C + M + 2) per side, confidence < 0.6 counts as unverifiable.
  Score[side].unanswered / .conceded count THAT side's points left unanswered / conceded by the opponent.

Notices (drain_notices -> Director.notice), each emitted at most once:
  unanswered     speaker = opposing speaker who should answer; detail {side, turns, importance, topic, text}
  answered       same speaker as its "unanswered"; detail {status: answered|conceded, by: [claim ids]}
  nonresponsive  speaker = the turn's speaker; claim_id = most important ignored point; detail {ignored, turn, side}
Snapshot extras beyond schemas.ArgMapSnapshot (extra="allow"): leader, margin, speakers {id: side};
MapNode.checkable/repeats/topic/utterance_id; Score.rated (= S + C + M).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app import config
from app.brain.types import Notice
from app.schemas import (ArgMapSnapshot, Claim, Link, MapEdge, MapNode, Pause, Score, SessionStart, Utterance,
                         Verdict)

SIDES = ("prop", "opp")
SIDE_LABEL = {"prop": "Proposition", "opp": "Opposition"}
ELIGIBLE = 0.5                 # importance >= ELIGIBLE: a point the other side is expected to answer
LOW_CONF = 0.6                 # verdicts below this count as unverifiable on the scoreboard
NO_CHECK = {"value", "prediction"}
DUP_WINDOW, DUP_JACCARD = 30, 0.7
LABELS = ("supported", "contradicted", "missing_context", "unverifiable")

_SIDE_ALIASES = {"prop": "prop", "proposition": "prop", "gov": "prop", "government": "prop", "pro": "prop",
                 "for": "prop", "aff": "prop", "affirmative": "prop",
                 "opp": "opp", "opposition": "opp", "con": "opp", "against": "opp", "neg": "opp",
                 "negative": "opp"}
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_TOK = re.compile(r"\d+(?:\.\d+)?|[a-z]+")
_NUM_WORDS = frozenset("two three four five six seven eight nine ten dozen hundred thousand million billion "
                       "trillion half third quarter twice double triple".split())
# negations and comparatives are deliberately NOT stopwords: "rose" vs "did not rise" must stay different
_STOP = frozenset("a an the and or but of to in on at for by with from as is are was were be been being it its "
                  "this that these those there their they them we our us you your i he she his her do does did "
                  "has have had so than then very just also about into which who whom what when where why how "
                  "such only own same too would will can could should may might must s t".split())


def _knob(name: str, default: Any) -> Any:
    return getattr(config, name, default)


def _norm_side(s: Any) -> Optional[str]:
    return _SIDE_ALIASES.get(str(s or "").strip().lower())


def _norm_importance(v: Any) -> float:
    """Contract is 0..1 and schemas.Claim already maps a 1..3 score; a raw value that bypassed validation is
    scaled the same way (x/3, capped at 1), like director._imp."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.5
    return min(v / 3.0, 1.0) if v > 1 else max(v, 0.0)


def _sig(text: str) -> tuple[frozenset, frozenset]:
    """(content tokens, number tokens) for duplicate detection. "40,000" == "40000", "70%" == "70 percent"."""
    t = _THOUSANDS.sub("", (text or "").lower()).replace("%", " percent ")
    toks = frozenset(w for w in _TOK.findall(t) if w not in _STOP)
    return toks, frozenset(w for w in toks if w[0].isdigit() or w in _NUM_WORDS)


def _key(links: Iterable[Link]) -> tuple:
    return tuple((l.claim_id, l.relation) for l in links)


@dataclass
class Node:
    claim: Claim                   # stored copy: side filled, importance normalised, links cleaned
    turn: int
    order: int                     # arrival order (stable "oldest -> newest")
    extra_links: list = field(default_factory=list)   # merged in from repeats; survive upserts
    verdict: Optional[str] = None
    confidence: Optional[float] = None
    repeats: int = 0
    status: str = "open"

    @property
    def id(self) -> str:
        return self.claim.id

    @property
    def side(self) -> str:
        return self.claim.side or "prop"

    @property
    def importance(self) -> float:
        return self.claim.importance

    def links(self) -> list[Link]:
        return list(self.claim.links) + self.extra_links


@dataclass
class Turn:
    idx: int
    speaker: str
    side: str
    t_start: float = 0.0
    t_end: float = 0.0
    claims: list = field(default_factory=list)        # claim ids, arrival order
    utts: list = field(default_factory=list)          # [(utterance_id, text)]
    closed: bool = False

    def substantive(self, min_s: float) -> bool:
        return bool(self.claims) or self.t_end - self.t_start >= min_s


class ArgMap:
    def __init__(self, k_turns: int = 1, *, min_turn_s: Optional[float] = None,
                 min_nr_turn_s: Optional[float] = None, backchannel_words: Optional[int] = None):
        self.k_turns = max(1, int(k_turns))
        self.min_turn_s = float(_knob("ARGMAP_MIN_TURN_S", 8.0) if min_turn_s is None else min_turn_s)
        self.min_nr_turn_s = float(_knob("ARGMAP_MIN_NR_TURN_S", 15.0) if min_nr_turn_s is None else min_nr_turn_s)
        self.backchannel_words = int(_knob("ARGMAP_BACKCHANNEL_WORDS", 3)
                                     if backchannel_words is None else backchannel_words)
        self.version: int = 0          # bump on every change (run.py coalesces snapshots on it)
        self._clear()

    # ------------------------------------------------------------------------------------ session
    def _clear(self) -> None:
        self.motion: str = ""
        self.sides: dict[str, str] = {}
        self.nodes: dict[str, Node] = {}
        self.turns: list[Turn] = []
        self.utt_turn: dict[str, int] = {}
        self.nr: dict[int, dict] = {}                 # turn idx -> non-responsive entry
        self._notices: list[Notice] = []
        self._asked: dict[str, str] = {}              # claim id -> speaker told to answer it
        self._answered: set[str] = set()
        self._nr_sent: set[int] = set()
        self._pending_verdicts: dict[str, Verdict] = {}
        self._held: Optional[Utterance] = None
        self._interrupted: dict[str, int] = {}        # held final's id -> idx of the turn it was said over
        self.consistency: dict[str, Any] = {}         # Consistency id -> event (consistency.py's findings)
        self._now = 0.0

    def reset(self, session: Optional[SessionStart] = None) -> None:
        self._clear()
        if session is not None:
            self.motion = session.motion or ""
            items = list((session.speakers or {}).items())
            for spk, info in items:          # declared sides first; a missing side never defaults to prop
                if _norm_side(getattr(info, "side", None)):
                    self.sides[spk] = _norm_side(info.side)
            for spk, _ in items:
                self.side_of(spk)
        self.version += 1

    def _next_side(self) -> str:
        n = {s: sum(v == s for v in self.sides.values()) for s in SIDES}
        return "opp" if n["opp"] < n["prop"] else "prop"

    def side_of(self, speaker: str) -> str:
        if speaker not in self.sides:
            self.sides[speaker] = self._next_side()
        return self.sides[speaker]

    def _peek_side(self, who: str) -> str:
        """Side for a speaker id (or a side name) without registering anyone."""
        if who in self.sides:
            return self.sides[who]
        return _norm_side(who) or self._next_side()

    # ------------------------------------------------------------------------------------ events
    def on_utterance(self, u: Utterance) -> None:
        if not u.final or not (u.text or "").strip():
            return
        self._clock(u.ts, u.t_end)
        if u.id in self.utt_turn:                       # re-sent final (replay / STT revision): text only
            t = self.turns[self.utt_turn[u.id]]
            t.utts = [(i, u.text if i == u.id else x) for i, x in t.utts]
            return self._changed()
        if self._held is not None and self._held.id == u.id:
            self._held = u
            return
        self.side_of(u.speaker)
        cur = self.turns[-1] if self.turns else None
        held, self._held = self._held, None             # a held backchannel dies unless its speaker goes on
        if cur is None or cur.speaker != u.speaker:
            if held is not None and held.speaker == u.speaker:
                cur = self._open_turn(u.speaker)
                self._append(cur, held)
            elif cur is not None and len(u.text.split()) < self.backchannel_words:
                self._held = u                          # "No!" over the speaker: not a turn (yet)
                self._interrupted[u.id] = cur.idx
                return
            else:
                cur = self._open_turn(u.speaker)
        self._append(cur, u)
        self._changed()

    def on_pause(self, p: Pause) -> None:
        """Provisional non-responsive check of the speaker's open turn. Never closes the turn."""
        cur = self.turns[-1] if self.turns else None
        if cur is None or cur.closed or cur.speaker != p.speaker:
            return
        self._clock(p.ts, p.t)
        before = self.nr.get(cur.idx)
        self._set_nr(cur, self._judge(cur))
        if self.nr.get(cur.idx) != before:
            self.version += 1

    def add_claim(self, c: Claim) -> None:
        """Add (or upsert by id) a claim node; links to unknown ids are dropped."""
        if c.speaker not in self.sides and _norm_side(c.side):
            self.sides[c.speaker] = _norm_side(c.side)
        stored = c.model_copy(update={"side": self.side_of(c.speaker),
                                      "importance": _norm_importance(c.importance),
                                      "checkable": bool(c.checkable) and c.type not in NO_CHECK,
                                      "links": self._clean_links(c.id, c.links)})
        node = self.nodes.get(c.id)
        if node is not None:
            if self._content(node.claim) == self._content(stored):
                return                                  # exact duplicate (e.g. replay): no change
            if stored.utterance_id != node.claim.utterance_id and stored.utterance_id in self.utt_turn:
                if c.id in self.turns[node.turn].claims:
                    self.turns[node.turn].claims.remove(c.id)
                node.turn = self.utt_turn[stored.utterance_id]
                self.turns[node.turn].claims.append(c.id)
            node.claim = stored
        else:
            # said over the other speaker (a held final): no turn of its own, so it must not open one (that
            # closed the speaker's turn mid-flow). Filed under the turn it interrupted until _append() adopts it.
            idx = None if stored.utterance_id in self.utt_turn else self._interrupted.get(stored.utterance_id)
            if idx is None:
                turn = self._turn_for(stored)
                idx = turn.idx
                turn.claims.append(c.id)
                tc = c.t_end or c.ts
                if tc and not turn.utts:                # claims-only turn: time it by its claims
                    turn.t_start = turn.t_start or tc
                    turn.t_end = max(turn.t_end, tc)
            node = self.nodes[c.id] = Node(claim=stored, turn=idx, order=len(self.nodes))
            v = self._pending_verdicts.pop(c.id, None)
            if v is not None:
                node.verdict, node.confidence = v.label, v.confidence
        self._clock(c.ts, c.t_end)
        self._changed()

    def on_verdict(self, v: Verdict) -> None:
        node = self.nodes.get(v.claim_id)
        if node is None:                                # verdict raced ahead of its claim
            self._pending_verdicts[v.claim_id] = v
            return
        node.verdict, node.confidence = v.label, v.confidence
        self._clock(v.ts)
        self.version += 1

    def add_consistency(self, k: Any) -> None:
        """A Consistency finding. A concession becomes a cross-side "supports" link (so the conceded point gets
        status conceded and the scoreboard counts it); a self-contradiction is counted on its side's scoreboard.
        Every finding is also a notice for the Director (it decides whether to say anything)."""
        if k.id in self.consistency or k.claim_id not in self.nodes or k.earlier_claim_id not in self.nodes:
            return
        self.consistency[k.id] = k
        node = self.nodes[k.claim_id]
        if k.kind == "concession" and (k.earlier_claim_id, "supports") not in set(_key(node.links())):
            node.extra_links += self._clean_links(k.claim_id, [Link(claim_id=k.earlier_claim_id, relation="supports")])
        self._clock(k.ts)
        self._notices.append(Notice("consistency", node.claim.speaker, self._now, k.claim_id,
                                    {"kind": k.kind, "earlier_claim_id": k.earlier_claim_id, "topic": k.topic,
                                     "side": node.side, "confidence": k.confidence, "id": k.id}))
        self._changed()

    def merge_repeat(self, claim_id: str, links: Iterable[Any]) -> None:
        node = self.nodes.get(claim_id)
        if node is None:
            return
        node.repeats += 1
        have = set(_key(node.links()))
        node.extra_links += [l for l in self._clean_links(claim_id, links) if (l.claim_id, l.relation) not in have]
        self._changed()

    def drain_notices(self) -> list[Notice]:
        out, self._notices = self._notices, []
        return out

    # ------------------------------------------------------------------------------------ queries
    def context_for(self, speaker: str, limit: int = 15) -> list[Claim]:
        """Rolling LLM context: up to 4 of the opponent's open/unanswered points, every claim of the opponent's
        latest turn, the last 8 claims overall. Priority in that order when capping; returned oldest -> newest.
        `speaker` may also be a side name ("prop"/"opp")."""
        me = self._peek_side(speaker)
        order = list(self.nodes.values())
        pending = sorted((n for n in order if n.side != me and n.status in ("unanswered", "open")),
                         key=lambda n: (n.status != "unanswered", -n.importance, -n.order))[:4]
        last = next((t for t in reversed(self.turns) if t.side != me and t.claims), None)
        picks = [n.id for n in pending] + (list(reversed(last.claims)) if last else [])
        picks += [n.id for n in reversed(order[-8:])]
        keep = list(dict.fromkeys(picks))[:max(0, limit)]
        return [n.claim for n in sorted((self.nodes[i] for i in keep), key=lambda n: n.order)]

    def turn_text(self, speaker: str, exclude_utterance_id: Optional[str] = None) -> list[str]:
        """Raw text of the speaker's current turn (the turn of exclude_utterance_id if known), up to the last 2
        utterances before the excluded one."""
        idx = self.utt_turn.get(exclude_utterance_id or "")
        if idx is not None and self.turns[idx].speaker == speaker:
            utts = self.turns[idx].utts
            utts = utts[:[i for i, _ in utts].index(exclude_utterance_id)]
        else:
            turn = next((t for t in reversed(self.turns) if t.speaker == speaker), None)
            utts = [x for x in (turn.utts if turn else []) if x[0] != exclude_utterance_id]
        return [text for _, text in utts][-2:]

    def find_duplicate(self, speaker: str, text: str) -> Optional[str]:
        """Same speaker's last 30 claims: identical number sets AND token Jaccard >= 0.7 (stopwords removed)."""
        toks, nums = _sig(text)
        mine = [n for n in self.nodes.values() if n.claim.speaker == speaker][-DUP_WINDOW:]
        for n in reversed(mine):
            t2, n2 = _sig(n.claim.text)
            if nums != n2:
                continue                                # "4%" vs "5%" is a correction, not a repeat
            union = toks | t2
            sim = len(toks & t2) / len(union) if union else float(text.strip().lower() == n.claim.text.strip().lower())
            if sim >= DUP_JACCARD:
                return n.id
        return None

    def snapshot(self) -> ArgMapSnapshot:
        score, call, leader, margin = self._scoreboard()
        nodes = [MapNode(claim_id=n.id, speaker=n.claim.speaker, side=n.side, text=n.claim.text, type=n.claim.type,
                         importance=n.importance, status=n.status, verdict=n.verdict, confidence=n.confidence,
                         turn=n.turn, checkable=n.claim.checkable, repeats=n.repeats, topic=n.claim.topic,
                         utterance_id=n.claim.utterance_id)
                 for n in self.nodes.values()]
        edges = [MapEdge(src=n.id, dst=l.claim_id, relation=l.relation)
                 for n in self.nodes.values() for l in n.links()]
        turns = [{"idx": t.idx, "speaker": t.speaker, "side": t.side, "claims": list(t.claims), "closed": t.closed,
                  "t_start": t.t_start, "t_end": t.t_end, "substantive": t.substantive(self.min_turn_s)}
                 for t in self.turns]
        nonresp = [dict(self.nr[i], ignored=list(self.nr[i]["ignored"])) for i in sorted(self.nr)]
        return ArgMapSnapshot(version=self.version, motion=self.motion, nodes=nodes, edges=edges, score=score,
                              call=call, turns=turns, nonresponsive=nonresp,
                              leader=leader, margin=margin, speakers=dict(self.sides))

    # ------------------------------------------------------------------------------------ internals
    def _clock(self, *ts: float) -> None:
        """Notices are stamped with the latest event time seen (never the wall clock)."""
        self._now = max([self._now] + [float(t) for t in ts if t])

    def _changed(self) -> None:
        self._recompute()
        self.version += 1

    def _open_turn(self, speaker: str) -> Turn:
        if self.turns:
            self.turns[-1].closed = True
        t = Turn(idx=len(self.turns), speaker=speaker, side=self.side_of(speaker))
        self.turns.append(t)
        return t

    def _append(self, turn: Turn, u: Utterance) -> None:
        t0, t1 = u.t_start or u.ts, u.t_end or u.ts or u.t_start
        turn.t_start = min(turn.t_start, t0) if turn.t_start else t0
        turn.t_end = max(turn.t_end, t1)
        turn.utts.append((u.id, u.text))
        self.utt_turn[u.id] = turn.idx
        if self._interrupted.pop(u.id, None) is not None:   # a held final made it into a turn after all
            self._adopt(turn, u.id)

    def _adopt(self, turn: Turn, utterance_id: str) -> None:
        """Claims already extracted from a held final move into the turn it ended up in."""
        for n in self.nodes.values():
            if n.claim.utterance_id == utterance_id and n.id not in self.turns[n.turn].claims:
                n.turn = turn.idx
                turn.claims.append(n.id)

    def _turn_for(self, c: Claim) -> Turn:
        if c.utterance_id in self.utt_turn:
            return self.turns[self.utt_turn[c.utterance_id]]
        cur = self.turns[-1] if self.turns else None
        if cur is not None and cur.speaker == c.speaker:
            return cur
        if self.utt_turn:                               # utterances flowing: late claim of the speaker's turn
            mine = next((t for t in reversed(self.turns) if t.speaker == c.speaker), None)
            if mine is not None:
                return mine
        return self._open_turn(c.speaker)               # claims-only replay: a speaker change opens a turn

    def _clean_links(self, cid: str, links: Iterable[Any]) -> list[Link]:
        out, seen = [], set()
        for l in links or []:
            try:
                l = l if isinstance(l, Link) else Link.model_validate(l)
            except Exception:
                continue
            k = (l.claim_id, l.relation)
            if l.claim_id != cid and l.claim_id in self.nodes and k not in seen:
                seen.add(k)
                out.append(Link(claim_id=l.claim_id, relation=l.relation))
        return out

    @staticmethod
    def _content(c: Claim) -> tuple:
        return (c.utterance_id, c.speaker, c.text, c.type, c.checkable, c.importance, c.topic, c.side,
                _key(c.links))

    def _answerer(self, side: str) -> str:
        """The opposing speaker who should answer a point by `side`: whoever spoke last for the other side."""
        t = next((t for t in reversed(self.turns) if t.side != side), None)
        if t is not None:
            return t.speaker
        return next((s for s, v in self.sides.items() if v != side), "")

    def _recompute(self) -> None:
        nodes = self.nodes
        resp: dict[str, list[tuple[str, str]]] = {}
        for n in nodes.values():
            for l in n.links():
                tgt = nodes.get(l.claim_id)
                if tgt is not None and tgt.side != n.side:
                    resp.setdefault(l.claim_id, []).append((n.id, l.relation))
        for n in nodes.values():
            r = resp.get(n.id, [])
            later = 0
            if n.importance < ELIGIBLE:
                n.status = "minor"
            elif any(rel == "attacks" for _, rel in r):
                n.status = "answered"
            elif r:
                n.status = "conceded"
            else:
                later = sum(1 for t in self.turns if t.side != n.side and t.idx > n.turn and t.closed
                            and t.substantive(self.min_turn_s))
                n.status = "unanswered" if later >= self.k_turns else "open"
            if n.status == "unanswered" and n.id not in self._asked:
                self._asked[n.id] = who = self._answerer(n.side)
                self._notices.append(Notice("unanswered", who, self._now, n.id,
                                            {"side": n.side, "turns": later, "importance": n.importance,
                                             "topic": n.claim.topic, "text": n.claim.text}))
            elif n.status in ("answered", "conceded") and n.id in self._asked and n.id not in self._answered:
                self._answered.add(n.id)
                self._notices.append(Notice("answered", self._asked[n.id], self._now, n.id,
                                            {"status": n.status, "by": [s for s, _ in r]}))
        for t in self.turns:
            if t.closed or t.idx in self.nr:            # open turns only re-judged after a Pause flagged them
                self._set_nr(t, self._judge(t))

    def _judge(self, t: Turn) -> Optional[dict]:
        prev = next((p for p in reversed(self.turns[:t.idx]) if p.side != t.side and p.claims), None)
        own = [c for c in t.claims if c in self.nodes]
        if prev is None or len(own) < 2:
            return None
        if not t.closed and t.t_end - t.t_start < self.min_nr_turn_s:
            return None
        replied = {l.claim_id for n in self.nodes.values() if n.side == t.side and n.turn < t.idx
                   for l in n.links()}
        targets = [c for c in prev.claims
                   if c in self.nodes and self.nodes[c].importance >= ELIGIBLE and c not in replied]
        if not targets:
            return None
        hit = set(prev.claims)
        if any(l.claim_id in hit for c in own for l in self.nodes[c].links()):
            return None
        return {"turn": t.idx, "side": t.side, "speaker": t.speaker, "ignored": targets, "final": t.closed}

    def _set_nr(self, t: Turn, r: Optional[dict]) -> None:
        if r is None:
            self.nr.pop(t.idx, None)
            return
        self.nr[t.idx] = r
        if r["final"] and t.idx not in self._nr_sent:
            self._nr_sent.add(t.idx)
            top = max(r["ignored"], key=lambda c: self.nodes[c].importance)
            self._notices.append(Notice("nonresponsive", t.speaker, self._now, top,
                                        {"ignored": list(r["ignored"]), "turn": t.idx, "side": t.side}))

    def _scoreboard(self) -> tuple[dict[str, Score], str, Optional[str], float]:
        score, raw, rated = {}, {}, {}
        for side in SIDES:
            mine = [n for n in self.nodes.values() if n.side == side]
            k = dict.fromkeys(LABELS, 0)
            for n in mine:
                if n.verdict is not None:
                    k[n.verdict if n.verdict in k and (n.confidence or 0) >= LOW_CONF else "unverifiable"] += 1
            s, c, m = k["supported"], k["contradicted"], k["missing_context"]
            raw[side], rated[side] = (s + 0.5 * m + 1) / (s + c + m + 2), s + c + m
            score[side] = Score(**k, unanswered=sum(n.status == "unanswered" for n in mine),
                                conceded=sum(n.status == "conceded" for n in mine), score=round(raw[side], 3),
                                self_contradictions=sum(k.kind == "self_contradiction" and k.side == side
                                                        for k in self.consistency.values()),
                                rated=rated[side])
        if min(rated.values()) < 2:
            return score, "Not enough checked claims yet", None, 0.0
        diff = round(raw["prop"] - raw["opp"], 6)
        if abs(diff) < 0.10:
            return score, "Too close to call on the evidence", None, abs(diff)
        lead = "prop" if diff > 0 else "opp"
        return (score, f"{SIDE_LABEL[lead]} ahead on the evidence ({sum(rated.values())} checked claims)",
                lead, round(abs(diff), 3))
