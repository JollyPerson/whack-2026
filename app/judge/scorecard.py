"""The judge's scorecard: fixed numbers per side from one session's record. Owner: B. Pure: no clock, no I/O, no LLM.

collect(events) -> Record: the current session (from the last SessionStart on), deduplicated the way the Bus history
keeps it: the latest final revision of each Utterance, the latest Claim per id and Verdict per claim, the newest
ArgMapSnapshot, and every BotLine and Interjection once.

scorecard(record) -> dict. Per side (prop/opp; a speaker's side comes from SessionStart.speakers, else Claim.side, else
the argument map, else the side with fewer speakers so far, Prop first):
- speaking_s, words, utterances: final Utterances (t_end - t_start, else words / WORDS_PER_S)
- claims, checkable: the side's Claims (checkable, and not a value or prediction)
- verdicts: the sourced Verdicts on the side's claims by label. Confidence below LOW_CONF counts as unverifiable,
  as on the argument map's scoreboard (low_conf counts those); with_source = verdicts that carry evidence
- ai_flagged / ai_confirmed: side-bot lines aimed at the side from Claude's own knowledge (sourced=False): kind
  challenge = flagged false or misleading, kind concede = confirmed (a surprising truth). Never counted as sourced
- from the final ArgMapSnapshot: unanswered (the side's points the opponent left unanswered), conceded_to (the side's
  points the opponent conceded), answered / conceded / ignored (the opponent's points the side attacked, conceded or
  left unanswered), nonresponsive (the side's finished non-responsive turns)
- interjections / bot_lines: the referee's and the side bots' interventions aimed at the side, by kind
- pressure: those that put the side on the spot (is_pressure: the referee's correction, context, prompt and source
  request; every bot line but a concession). Not pressure: the referee's confirmations and a bot conceding a point
  (confirmations: in the side's favour; the Director voices confirms on purpose to balance its pressure) and time
  calls (procedure). With the flagged claims per side, pressure shows whether the SYSTEM was even-handed
  (scorecard()["system"], balance_text())

Derived 0-10 criteria, smoothed so a side with little on the record is not rated on one data point (S/C/M/U =
supported, contradicted, missing_context, unverifiable; AI knowledge at half weight, AI_WEIGHT). Saying nothing
checkable never pays: it earns no evidence, and balance starts below the top until checks bear it out.
  factuality      (S' + 0.5 M + 1) / (S' + C' + M + 2)     S' = S + 0.5 confirmed, C' = C + 0.5 flagged
  evidence        (S + 0.5 M) / (n + 1)                     n = max(checkable, S + C + M + U): how much of what the
                  side asserted a source backed (a side that sourced nothing scores 0)
  responsiveness  (answered + conceded + 1) / (answered + conceded + ignored + 2) - 0.1 per non-responsive turn
  balance         1 - (M + 0.5 flagged + 0.5) / (S + C + M + 0.5 (flagged + confirmed) + 2): fewer misleading or
                  cherry-picked flags = higher; 7.5 with nothing checked
overall = 0.35 factuality + 0.25 evidence + 0.25 responsiveness + 0.15 balance (WEIGHTS), from the criteria rounded to
0.1. formula(): an overall difference below DRAW_BELOW is a draw (the margin is narrow), below DECISIVE_FROM it is
clear, from there decisive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.brain.argmap import LOW_CONF, NO_CHECK, SIDE_LABEL, SIDES
from app.brain.director import PRESSURE, norm_side
from app.schemas import ArgMapSnapshot, BotLine, Claim, Event, Interjection, SessionStart, Utterance, Verdict

WEIGHTS = {"factuality": 0.35, "evidence": 0.25, "responsiveness": 0.25, "balance": 0.15}
CRITERIA = tuple(WEIGHTS)
DRAW_BELOW = 0.5          # |prop - opp| overall below this: a narrow margin, declared a draw
DECISIVE_FROM = 2.0       # this or more: decisive; in between: clear
AI_WEIGHT = 0.5           # an unsourced AI-knowledge result counts half as much as a sourced verdict
NR_PENALTY = 0.1          # responsiveness lost per finished non-responsive turn
BALANCE_PRIOR = 0.5       # balance with nothing checked: 1 - 0.5 / 2 = 7.5, not a perfect 10
WORDS_PER_S = 2.5         # speaking time of an utterance without usable times
MAX_UTTERANCE_S = 120.0   # longer than this: a time from another clock, use the word count instead
LABELS = ("supported", "contradicted", "missing_context", "unverifiable")
OTHER = {"prop": "opp", "opp": "prop"}
SECOND = {"for": "prop", "against": "opp"}    # the side each bot seconds
AIMED_AT = {"for": "opp", "against": "prop"}  # each bot only ever addresses the opposing debater
PRESSURE_KINDS = PRESSURE - {"time"}          # referee kinds that put a side on the spot (a time call is procedure)
CONFIRM_KINDS = {"confirm", "concede"}        # the referee confirming a claim, a bot conceding one: in its favour


@dataclass
class Record:
    """One session's events, deduplicated. `sides`/`names`: speaker id -> prop|opp / display name."""
    motion: str = ""
    start: Optional[SessionStart] = None
    utterances: list[Utterance] = field(default_factory=list)
    claims: dict[str, Claim] = field(default_factory=dict)
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    bot_lines: list[BotLine] = field(default_factory=list)
    interjections: list[Interjection] = field(default_factory=list)
    snapshot: Optional[ArgMapSnapshot] = None
    sides: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def name(self, speaker: Optional[str]) -> str:
        return self.names.get(speaker or "") or str(speaker or "?")

    def claim_side(self, c: Claim) -> Optional[str]:
        return norm_side(c.side) or self.sides.get(c.speaker)

    def aimed_at(self, ev: Any) -> Optional[str]:
        """The side a BotLine or Interjection addresses: its target's. Without a target, a bot line addresses the
        side its bot opposes (a nudge's claim is the bot's OWN side's point) and an interjection its claim's side.
        None: aimed at nobody (banter, a time call to the room). Not the claim first: "Opposition, you haven't
        answered..." carries the Proposition's claim."""
        side = self.sides.get(getattr(ev, "target", None) or "")
        if side is None and isinstance(ev, BotLine):
            side = AIMED_AT.get(ev.persona) if ev.kind != "banter" else None
        elif side is None:
            c = self.claims.get(getattr(ev, "claim_id", None) or "")
            side = self.claim_side(c) if c is not None else None
        return side


def collect(events: Iterable[Event]) -> Record:
    """The current session's record from events in publish order (the Bus history, or a JSONL log)."""
    evs = list(events)
    starts = [i for i, e in enumerate(evs) if e.event == "SessionStart"]
    rec = Record()
    utts: dict[str, Utterance] = {}
    lines: dict[str, BotLine] = {}
    inters: dict[str, Interjection] = {}
    for ev in evs[starts[-1]:] if starts else evs:
        name = ev.event
        if name == "SessionStart":
            rec.start, rec.motion = ev, ev.motion
        elif name == "Utterance":
            if getattr(ev, "final", True) and ev.text.strip():
                utts[ev.id] = ev                     # a revised final replaces the first, in the first one's place
        elif name == "Claim":
            rec.claims[ev.id] = ev
        elif name == "Verdict":
            rec.verdicts[ev.claim_id] = ev           # the latest verdict per claim (a deep check may revise one)
        elif name == "BotLine":
            lines.setdefault(ev.id, ev)
        elif name == "Interjection":
            inters.setdefault(ev.id, ev)
        elif name == "ArgMapSnapshot" and (rec.snapshot is None or ev.version >= rec.snapshot.version):
            rec.snapshot = ev
    rec.utterances, rec.bot_lines, rec.interjections = list(utts.values()), list(lines.values()), list(inters.values())
    if not rec.motion and rec.snapshot is not None:
        rec.motion = rec.snapshot.motion
    _assign_sides(rec)
    return rec


def _assign_sides(rec: Record) -> None:
    hints: list[tuple[str, Any]] = []
    if rec.start is not None:
        for spk, info in rec.start.speakers.items():
            if info.name:
                rec.names[spk] = info.name
            hints.append((spk, info.side))
    hints += [(c.speaker, c.side) for c in rec.claims.values()]
    if rec.snapshot is not None:
        hints += list((getattr(rec.snapshot, "speakers", None) or {}).items())
        hints += [(n.speaker, n.side) for n in rec.snapshot.nodes]
    for spk, side in hints:
        if norm_side(side) and spk:
            rec.sides.setdefault(spk, norm_side(side))
    for u in rec.utterances:                         # nobody said: the side with fewer speakers, Prop first
        if u.speaker not in rec.sides:
            count = {s: sum(v == s for v in rec.sides.values()) for s in SIDES}
            rec.sides[u.speaker] = min(SIDES, key=lambda s: count[s])


def speech_s(u: Utterance) -> float:
    d = (u.t_end - u.t_start) if u.t_start and u.t_end else 0.0
    return d if 0 < d <= MAX_UTTERANCE_S else len(u.text.split()) / WORDS_PER_S


def _side_blank(rec: Record, side: str) -> dict:
    return {"label": SIDE_LABEL[side],
            "speakers": [rec.name(s) for s, sd in rec.sides.items() if sd == side],
            "speaking_s": 0.0, "words": 0, "utterances": 0, "claims": 0, "checkable": 0,
            "verdicts": {**dict.fromkeys(LABELS, 0), "with_source": 0, "low_conf": 0},
            "ai_flagged": 0, "ai_confirmed": 0,
            "unanswered": 0, "conceded_to": 0, "answered": 0, "conceded": 0, "ignored": 0, "nonresponsive": 0,
            "interjections": {"total": 0}, "bot_lines": {"total": 0}, "pressure": 0, "confirmations": 0}


def _bump(d: dict, kind: str) -> None:
    d["total"] += 1
    d[kind] = d.get(kind, 0) + 1


def is_pressure(ev: Any) -> bool:
    """An intervention that puts the side it addresses on the spot. A bot line is, unless it concedes (or is banter,
    which addresses nobody); a referee interjection only for PRESSURE_KINDS (not a confirmation or a time call)."""
    if isinstance(ev, BotLine):
        return ev.kind not in ("concede", "banter")
    return ev.kind in PRESSURE_KINDS


def _intervention(st: dict, key: str, ev: Any) -> None:
    _bump(st[key], ev.kind)
    st["pressure"] += is_pressure(ev)
    st["confirmations"] += ev.kind in CONFIRM_KINDS


def checkable(c: Claim) -> bool:
    return bool(c.checkable) and c.type not in NO_CHECK


def counted_label(v: Verdict) -> str:
    """The label as the scorecard counts it: below LOW_CONF a verdict is unverifiable."""
    return v.label if v.label == "unverifiable" or v.confidence >= LOW_CONF else "unverifiable"


def scorecard(rec: Record) -> dict:
    sides = {s: _side_blank(rec, s) for s in SIDES}
    for u in rec.utterances:
        st = sides.get(rec.sides.get(u.speaker, ""))
        if st is not None:
            st["speaking_s"] += speech_s(u)
            st["words"] += len(u.text.split())
            st["utterances"] += 1
    for c in rec.claims.values():
        st = sides.get(rec.claim_side(c) or "")
        if st is None:
            continue
        st["claims"] += 1
        st["checkable"] += checkable(c)
        v = rec.verdicts.get(c.id)
        if v is not None:
            label = counted_label(v)
            st["verdicts"][label] += 1
            st["verdicts"]["low_conf"] += label != v.label
            st["verdicts"]["with_source"] += bool(v.evidence)
    banter = 0
    for b in rec.bot_lines:
        st = sides.get(rec.aimed_at(b) or "")
        if st is None:
            banter += 1
            continue
        _intervention(st, "bot_lines", b)
        if not b.sourced and b.kind == "challenge":
            st["ai_flagged"] += 1
        elif not b.sourced and b.kind == "concede":
            st["ai_confirmed"] += 1
    room = 0
    for i in rec.interjections:
        st = sides.get(rec.aimed_at(i) or "")
        if st is None:
            room += 1
        else:
            _intervention(st, "interjections", i)
    snap = rec.snapshot
    for n in snap.nodes if snap is not None else []:
        side = norm_side(n.side) or rec.sides.get(n.speaker)
        if side not in sides:
            continue
        own, other = sides[side], sides[OTHER[side]]
        if n.status == "unanswered":
            own["unanswered"] += 1
            other["ignored"] += 1
        elif n.status == "conceded":
            own["conceded_to"] += 1
            other["conceded"] += 1
        elif n.status == "answered":
            other["answered"] += 1
    for nr in snap.nonresponsive if snap is not None else []:
        side = norm_side(nr.get("side"))
        if side in sides and nr.get("final", True):
            sides[side]["nonresponsive"] += 1
    talk = sum(st["speaking_s"] for st in sides.values()) or 1.0
    for st in sides.values():
        st["speaking_s"] = round(st["speaking_s"], 1)
        st["share"] = round(st["speaking_s"] / talk, 2)
        st["criteria"] = criteria(st)
        st["overall"] = overall(st["criteria"])
    return {"motion": rec.motion,
            "speakers": [{"id": s, "name": rec.name(s), "side": sd} for s, sd in rec.sides.items()],
            "sides": sides, "system": system_balance(sides, banter, room), "formula": formula(sides)}


def _unit(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def criteria(st: dict) -> dict[str, float]:
    v = st["verdicts"]
    S, C, M, U = v["supported"], v["contradicted"], v["missing_context"], v["unverifiable"]
    flagged, confirmed = st["ai_flagged"], st["ai_confirmed"]
    s_ai, c_ai = S + AI_WEIGHT * confirmed, C + AI_WEIGHT * flagged
    replied = st["answered"] + st["conceded"]
    raw = {
        "factuality": (s_ai + 0.5 * M + 1) / (s_ai + c_ai + M + 2),
        "evidence": (S + 0.5 * M) / (max(st["checkable"], S + C + M + U) + 1),
        "responsiveness": (replied + 1) / (replied + st["ignored"] + 2) - NR_PENALTY * st["nonresponsive"],
        "balance": 1 - (M + AI_WEIGHT * flagged + BALANCE_PRIOR) / (S + C + M + AI_WEIGHT * (flagged + confirmed) + 2),
    }
    return {k: round(10 * _unit(raw[k]), 1) for k in CRITERIA}


def overall(crit: dict) -> float:
    return round(sum(w * float(crit.get(k, 0.0)) for k, w in WEIGHTS.items()), 1)


def margin_of(gap: float) -> str:
    """narrow (a draw) | clear | decisive, for an overall difference on the 0-10 scale (rounded to 0.1, like the
    overalls themselves)."""
    gap = round(abs(gap), 1)
    return "narrow" if gap < DRAW_BELOW else "clear" if gap < DECISIVE_FROM else "decisive"


def formula(sides: dict) -> dict:
    """The scorecard's own verdict: winner prop|opp|draw and margin ("" for a draw)."""
    ov = {s: sides[s]["overall"] for s in SIDES}
    diff = round(ov["prop"] - ov["opp"], 1)
    margin = margin_of(diff)
    winner = "draw" if margin == "narrow" else "prop" if diff > 0 else "opp"
    return {"winner": winner, "margin": "" if winner == "draw" else margin, "overall": ov,
            "difference": abs(diff), "weights": dict(WEIGHTS), "draw_below": DRAW_BELOW,
            "decisive_from": DECISIVE_FROM}


def flagged(st: dict) -> int:
    """Claims the system found fault with: contradicted or missing context (sourced), or flagged by AI knowledge."""
    return st["verdicts"]["contradicted"] + st["verdicts"]["missing_context"] + st["ai_flagged"]


def system_balance(sides: dict, banter: int = 0, room: int = 0) -> dict:
    """Was the referee's and the bots' PRESSURE even? Counted per side next to the claims each side had flagged:
    more pressure on the side with more flagged claims is fair, not bias. Confirmations (in a side's favour) and time
    calls (procedure) are not pressure; confirmations are counted apart, time calls stay in the by-kind counts."""
    inter = {s: sides[s]["interjections"]["total"] for s in SIDES}
    bots = {s: sides[s]["bot_lines"]["total"] for s in SIDES}
    total = {s: sides[s]["pressure"] for s in SIDES}
    flags = {s: flagged(sides[s]) for s in SIDES}
    hi, lo = max(SIDES, key=lambda s: total[s]), min(SIDES, key=lambda s: total[s])
    even = total[hi] - total[lo] <= 1 or total[hi] <= 1.5 * total[lo]
    if even:
        note = f"even ({total['prop']} on the Proposition, {total['opp']} on the Opposition)"
    else:
        fair = flags[hi] > flags[lo]
        note = (f"more on the {SIDE_LABEL[hi]} ({total[hi]} to {total[lo]}), "
                + ("in line with its flagged claims" if fair else "although it did not have more flagged claims")
                + f" ({flags[hi]} to {flags[lo]})")
    return {"interjections": {**inter, "none": room}, "bot_lines": {**bots, "banter": banter},
            "pressure": total, "confirmations": {s: sides[s]["confirmations"] for s in SIDES},
            "time_calls": {s: sides[s]["interjections"].get("time", 0) for s in SIDES},
            "flagged": flags, "even": even, "note": note}


def balance_text(system: dict) -> str:
    """The system's balance in a sentence or two, for the report and the formula's bias conclusion."""
    out = (f"The referee's and the side bots' pressure (corrections, prompts, challenges and the like) was "
           f"{system['note']}.")
    conf, times = system["confirmations"], system["time_calls"]
    if any(conf.values()):
        out += (f" Confirmations, which favour the side and are not pressure: Proposition {conf['prop']}, "
                f"Opposition {conf['opp']}.")
    if any(times.values()):
        out += f" Time calls (procedure, not pressure): Proposition {times['prop']}, Opposition {times['opp']}."
    return out


def fact_checks(rec: Record) -> dict:
    """The record's fact checks, sourced and AI knowledge kept apart. `unchecked`: claims with no verdict."""
    sourced = []
    for c in rec.claims.values():
        v = rec.verdicts.get(c.id)
        if v is None:
            continue
        sourced.append({"claim_id": c.id, "side": rec.claim_side(c), "speaker": rec.name(c.speaker), "text": c.text,
                        "label": v.label, "counted_as": counted_label(v), "confidence": round(v.confidence, 2),
                        "explanation": v.explanation, "via": v.via,
                        "sources": [{"title": e.title, "url": e.url, "quote": e.quote} for e in v.evidence]})
    ai = []
    for b in rec.bot_lines:
        if b.sourced or b.kind not in ("challenge", "concede"):
            continue
        c = rec.claims.get(b.claim_id or "")
        ai.append({"persona": b.persona, "side": rec.aimed_at(b), "kind": b.kind, "text": b.text,
                   "claim_id": b.claim_id, "claim": c.text if c is not None else ""})
    return {"sourced": sourced, "ai": ai, "unchecked": sum(c.id not in rec.verdicts for c in rec.claims.values())}
