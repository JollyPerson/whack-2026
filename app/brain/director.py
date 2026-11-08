"""Director: decides whether, when, about what and to whom the referee speaks. Owner: B.

Pure core: no I/O, no clock, no randomness. `now` is always passed in and all state lives in `State`,
so the same events with the same `now`s always give the same decisions (interjection ids aside).

    Director.handle(ev, now)  Claim, Verdict, Utterance (interim+final), Pause, Control, TTSState, SessionStart
    Director.notice(n, now)   argument-map findings: unanswered / answered / nonresponsive
    Director.tick(now)        every 250 ms: TTL expiry + fire when the floor opens
    Director.status(now)      DirectorStatus heartbeat ("why is it silent?")
  -> [Interjection (what the referee says), DirectorDecision (state transitions, for the log panel)]

Gates, in order, on every event and tick: G0 TTL, G1 own TTS, G2 floor open, G3 fresh human pause,
G4 threshold, G5 cooldown, G6 budget, G7 balance streak, then mute. force_next skips G4-G7 only: the
referee never talks over a human or itself, and never twice without a human pause in between.
Balance reorders or defers real candidates; it never invents one or holds back a strong correction.
Speaker ids are opaque. Sides come from SessionStart, else first speaker heard = prop, second = opp, ...
Producer times (t_start/t_end/t) far from the envelope ts (stream-relative STT seconds, another clock) are
treated as unknown. Until an interim Utterance is seen, a final alone never proves the speaker stopped.
Wording lives in voice.py.
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Union

from app import config
from app.brain import voice
from app.brain.types import Notice
from app.schemas import (Claim, Control, DirectorDecision, DirectorStatus, Event, Interjection, Pause, SessionStart,
                         TTSState, Utterance, Verdict, parse_event)

KINDS = ("time", "correction", "context", "prompt", "source_req", "confirm")   # order = tie-break rank
KIND_W = {"time": .9, "correction": 1.0, "context": .7, "prompt": .6, "source_req": .55, "confirm": .35}
# prompt/source_req carry no model confidence (conf 1.0): KIND_W alone ranks them under a confident context
# of equal importance. At aggr 3 an importance-0.67 prompt stays >= .35 for ~12 s after detection (2x half-life:
# the argmap only notices once the next speaker is already talking), an importance-0.8 source request ~10 s
# after the claim. Both stay under override_score/streak_override, so cooldown and balance still bind them.
HL_MULT = {"prompt": 2.0}
TTL_S = {"time": 15, "correction": 45, "context": 40, "prompt": 90, "source_req": 30, "confirm": 25}
PRESSURE = {"time", "correction", "context", "prompt", "source_req"}   # kinds that put a speaker on the spot
LABEL2KIND = {"contradicted": "correction", "missing_context": "context", "supported": "confirm"}
AGGR = {1: dict(min_conf=.85, fire_threshold=.55, min_gap_s=60.0), 2: dict(min_conf=.80, fire_threshold=.45, min_gap_s=45.0),
        3: dict(min_conf=.75, fire_threshold=.35, min_gap_s=30.0), 4: dict(min_conf=.70, fire_threshold=.28, min_gap_s=20.0),
        5: dict(min_conf=.65, fire_threshold=.22, min_gap_s=15.0)}
MIN_CONF_FLOOR = .65               # no aggressiveness level or env override goes below this
SKEW_S = 120.0                     # a producer time this far from its envelope ts is on another clock
SIDE_ALIASES = {"prop": "prop", "proposition": "prop", "gov": "prop", "government": "prop", "pro": "prop",
                "for": "prop", "aff": "prop", "affirmative": "prop", "opp": "opp", "opposition": "opp",
                "con": "opp", "against": "opp", "neg": "opp", "negative": "opp"}   # same as argmap
NEG = float("-inf")
_IDS = itertools.count(1)          # process-global: interjection ids never repeat across sessions


@dataclass(frozen=True)
class Cfg:
    min_conf: float = .75; confirm_min_conf: float = .85; min_importance: float = .4
    srcreq_min_importance: float = .8      # unverifiable -> neutral source request, statistics only
    prompt_min_importance: float = .66     # unanswered-point prompts: LLM importance 2 (0.67) and up
    prompt_conf: float = 1.0; srcreq_conf: float = 1.0
    fire_threshold: float = .35; half_life_s: float = 30.0
    min_gap_s: float = 30.0; hard_gap_s: float = 12.0; override_score: float = .80
    budget_n: int = 6; budget_window_s: float = 300.0
    max_streak: int = 2; streak_override: float = .75; confirm_boost: float = 1.6; confirm_boost_gap: int = 2
    post_tts_s: float = .5; tts_cap_s: float = 8.0; words_per_s: float = 2.5; force_window_s: float = 20.0
    floor_timeout_s: float = 2.5           # no Utterance this long == paused, so one dropped Pause can't mute us
    # Finals-only ears (no interim seen yet): the next final may be a whole sentence/chunk away, so wait
    # max(finals_timeout_s, last final's length + floor_timeout_s), capped. A real Pause still opens at once.
    finals_timeout_s: float = 6.0; finals_timeout_max_s: float = 15.0


@dataclass
class Cand:
    key: str                               # dedupe key "<kind>:<canonical_key or claim id>"
    kind: str
    target: str                            # speaker id addressed
    side: str
    conf: float
    imp: float
    t0: float                              # when it was said (prompt: detected, time: requested)
    claim_id: Optional[str] = None
    claim: Optional[Claim] = None
    verdict: Optional[Verdict] = None
    score: float = 0.0


@dataclass
class State:
    cfg: Cfg = field(default_factory=Cfg)
    aggr: int = 3
    muted: bool = False                               # Control mute
    director_off: bool = False                        # Control director=false (separate: mute=false can't undo it)
    overrides: dict = field(default_factory=dict)     # operator: speaker id or side -> spoken name
    sides: dict = field(default_factory=dict)         # speaker -> side (insertion order = first seen)
    speaker_names: dict = field(default_factory=dict) # speaker -> SessionStart name (last-resort spoken name)
    claims: dict = field(default_factory=dict)        # claim_id -> Claim
    verdicts: dict = field(default_factory=dict)      # claim_id -> latest Verdict (also ones that beat their Claim)
    pending: dict = field(default_factory=dict)       # key -> Cand allowed to fire
    spoken: set = field(default_factory=set)          # keys already voiced: never twice per session
    voiced: set = field(default_factory=set)          # claim ids that already got a verdict-based line
    vetoed: set = field(default_factory=set)          # operator vetoes: keys or claim ids
    fires: list = field(default_factory=list)         # [(t, target, side, kind)]
    pressure: dict = field(default_factory=lambda: defaultdict(int))   # speaker -> times put on the spot
    last_speech: dict = field(default_factory=dict)   # speaker -> latest speech evidence (t_start/t_end)
    last_heard: dict = field(default_factory=dict)    # speaker -> latest time any Utterance arrived
    last_pause: dict = field(default_factory=dict)    # speaker -> latest Pause.t
    final_dur: dict = field(default_factory=dict)     # speaker -> length of their latest final Utterance
    interims_seen: bool = False
    tts_until: float = NEG
    force_until: float = NEG
    last_log: dict = field(default_factory=dict)      # key -> (action, reason): log transitions only


# ----------------------------------------------------------------------------- config
def clamp_aggr(v: Any, cur: int = 3) -> int:
    """1..5 from an int, a numeric string, or relative "+"/"-"/"up"/"down"; anything else keeps `cur`."""
    if isinstance(v, str) and v.strip().lower() in ("+", "up", "-", "down"):
        v = cur + (1 if v.strip().lower() in ("+", "up") else -1)
    if isinstance(v, bool):
        return cur
    try:
        return max(1, min(5, int(float(v))))
    except (TypeError, ValueError):
        return cur


def level_cfg(aggr: int, base: Optional[Cfg] = None) -> Cfg:
    """Cfg for an aggressiveness level. Level 3 takes DIRECTOR_MIN_CONF / DIRECTOR_MIN_GAP_S from config/env."""
    lv = dict(AGGR[clamp_aggr(aggr)])
    if clamp_aggr(aggr) == 3:
        lv["min_conf"] = float(getattr(config, "DIRECTOR_MIN_CONF", lv["min_conf"]))
        lv["min_gap_s"] = float(getattr(config, "DIRECTOR_MIN_GAP_S", lv["min_gap_s"]))
    lv["min_conf"] = max(MIN_CONF_FLOOR, lv["min_conf"])
    lv["finals_timeout_s"] = float(getattr(config, "FINALS_ONLY_WAIT_S", Cfg.finals_timeout_s))
    return replace(base or Cfg(), **lv)


def truthy(v: Any) -> bool:
    """Control values from any client: "false"/"0"/"off"/"" are False (bool("false") is True)."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def on_clock(t: float, ref: float) -> float:
    """A producer time if it is near `ref` (the envelope ts), else `ref`: 0 = unknown, and a time far off is
    stream-relative (STT start/end seconds) or another machine's clock."""
    return t if t and abs(t - ref) <= SKEW_S else ref


# ----------------------------------------------------------------------------- speakers
def norm_side(s: Any) -> Optional[str]:
    return SIDE_ALIASES.get(str(s or "").strip().lower())


def side_of(st: State, spk: str, hint: Optional[str] = None) -> str:
    """Registers unknown speakers: hint (Claim.side, aliases ok) if valid, else the side with fewer speakers so
    far (first seen = prop, then opp, ...)."""
    if spk not in st.sides:
        n = {s: sum(v == s for v in st.sides.values()) for s in ("prop", "opp")}
        st.sides[spk] = norm_side(hint) or ("opp" if n["opp"] < n["prop"] else "prop")
    return st.sides[spk]


def spoken_name(st: State, spk: str) -> str:
    side = st.sides.get(spk)
    for k in (spk, side):
        if k is not None and st.overrides.get(k):
            return str(st.overrides[k])
    return voice.SIDE_LABEL.get(side or "") or st.speaker_names.get(spk) or str(spk)


def _side_pressure(st: State, side: str, same: bool = True) -> int:
    return sum(p for s, p in st.pressure.items() if (st.sides.get(s) == side) == same)


# ----------------------------------------------------------------------------- floor model
def _paused_at(st: State, spk: str, now: float) -> Optional[float]:
    """When `spk` last went quiet: their Pause if no speech is known after it, else a floor timeout."""
    p = st.last_pause.get(spk, NEG)
    if p >= st.last_speech.get(spk, NEG):
        return p
    c, heard = st.cfg, st.last_heard.get(spk, NEG)
    to = c.floor_timeout_s if st.interims_seen else \
        min(max(c.finals_timeout_s, st.final_dur.get(spk, 0.0) + c.floor_timeout_s), c.finals_timeout_max_s)
    return heard + to if now - heard >= to else None


def floor_open(st: State, now: float) -> bool:
    return bool(st.last_speech) and all(_paused_at(st, s, now) is not None for s in st.last_speech)


def _latest_pause(st: State, now: float) -> float:
    ts = [_paused_at(st, s, now) for s in {*st.last_speech, *st.last_pause}]
    return max([t for t in ts if t is not None], default=NEG)


# ----------------------------------------------------------------------------- inputs
def on_event(st: State, ev: Event, now: float) -> list[Event]:
    out: list[Event] = []
    if isinstance(ev, Claim):
        side_of(st, ev.speaker, ev.side)
        if ev.t_end and on_clock(ev.t_end, ev.ts or now) != ev.t_end:
            ev = ev.model_copy(update={"t_end": ev.ts or now})
        old, st.claims[ev.id] = st.claims.get(ev.id), ev
        if ev.id in st.verdicts and (old is None or _content(old) != _content(ev)):
            _on_verdict(st, st.verdicts[ev.id], out, now)    # its verdict came first, or the claim was re-sent changed
    elif isinstance(ev, Verdict):
        _on_verdict(st, ev, out, now)
    elif isinstance(ev, Utterance):    # interim or final: evidence the speaker was talking until t_end
        side_of(st, ev.speaker)
        t = min(on_clock(max(ev.t_start, ev.t_end), ev.ts or now), now)   # never later than arrival (clock skew)
        st.last_speech[ev.speaker] = max(st.last_speech.get(ev.speaker, NEG), t)
        st.last_heard[ev.speaker] = max(st.last_heard.get(ev.speaker, NEG), now)
        st.interims_seen |= not ev.final
        if ev.final and ev.t_start and ev.t_end:
            st.final_dur[ev.speaker] = max(0.0, ev.t_end - ev.t_start)
    elif isinstance(ev, Pause):
        side_of(st, ev.speaker)
        st.last_pause[ev.speaker] = max(st.last_pause.get(ev.speaker, NEG), min(on_clock(ev.t, ev.ts or now), now))
    elif isinstance(ev, TTSState):
        _tts(st, truthy(ev.speaking), now)
    elif isinstance(ev, Control):
        _on_control(st, ev, out, now)
    else:
        return out
    return out + try_fire(st, now)


def on_notice(st: State, n: Notice, now: float) -> list[Event]:
    out: list[Event] = []
    if n.kind == "unanswered":
        _prompt(st, n.claim_id, n.speaker, n.t or now, out, now)
    elif n.kind == "answered":
        cd = st.pending.pop(f"prompt:{n.claim_id}", None)
        if cd:
            _log(st, out, cd, "dropped", "answered")
    elif n.kind == "nonresponsive":
        detail = n.detail or {}
        ignored = [st.claims[i] for i in detail.get("ignored", []) if i in st.claims]
        top = min(ignored, key=lambda c: (-_imp(c), c.id)) if ignored else None
        cid = top.id if top else None
        if truthy(getattr(config, "DIRECTOR_VOICE_NONRESPONSIVE", False)):
            _prompt(st, cid, n.speaker, n.t or now, out, now)
        else:
            turn = detail.get("turn", round(n.t or now, 1))
            key = f"nonresponsive:{n.speaker}:{turn}"
            _log(st, out, Cand(key, "prompt", n.speaker, side_of(st, n.speaker), st.cfg.prompt_conf,
                               _imp(top) if top else 0.0, n.t or now, cid, top), "skipped", "dashboard_only")
    return out + try_fire(st, now)


def _imp(cl: Claim) -> float:
    x = float(cl.importance or 0.0)
    return min(x / 3.0, 1.0) if x > 1 else max(x, 0.0)      # tolerate a raw 1..3 LLM score


def _t0(cl: Claim, now: float) -> float:
    return cl.t_end or cl.marks.get("speech_end") or cl.ts or now


def _content(cl: Claim) -> tuple:
    return (cl.speaker, cl.side, cl.text, cl.type, cl.checkable, cl.importance, cl.topic, cl.canonical_key, cl.t_end)


def _on_verdict(st: State, v: Verdict, out: list, now: float) -> None:
    st.verdicts[v.claim_id] = v                   # the latest verdict wins (index, then web)
    c, cl = st.cfg, st.claims.get(v.claim_id)
    if cl is None:
        return              # raced ahead of its Claim: applied when the Claim arrives (TTL still counts from t_end)
    for k in [k for k, p in st.pending.items() if p.claim_id == cl.id and p.kind != "prompt"]:
        _log(st, out, st.pending.pop(k), "dropped", "superseded")    # the newer verdict wins
    kind, conf, why, imp = LABEL2KIND.get(v.label), float(v.confidence or 0.0), None, _imp(cl)
    if v.label == "unverifiable":                 # NEVER a correction; at most a neutral source request
        kind, conf = "source_req", c.srcreq_conf
        why = None if cl.type == "statistic" and imp >= c.srcreq_min_importance else "unverifiable_minor"
    elif kind is None:
        return
    elif not cl.checkable:
        why = "not_checkable"
    elif "capped" in (getattr(v, "guard_notes", None) or ()):
        why = "capped"          # C's guards kept it off air: dashboard only, at every aggressiveness (aggr 4-5 go < .74)
    elif conf < (c.confirm_min_conf if kind == "confirm" else c.min_conf):
        why = "low_conf"
    elif not v.evidence:
        why = "no_source"
    _add(st, Cand(f"{kind}:{cl.canonical_key or cl.id}", kind, cl.speaker, side_of(st, cl.speaker, cl.side),
                  conf, imp, _t0(cl, now), cl.id, cl, v), out, now, why)


def _prompt(st: State, cid: Optional[str], spk: str, t: float, out: list, now: float) -> None:
    cl = st.claims.get(cid) if cid else None
    cd = Cand(f"prompt:{cid}", "prompt", spk, side_of(st, spk), st.cfg.prompt_conf, _imp(cl) if cl else 0.0, t, cid, cl)
    why = "unknown_claim" if cl is None else "low_importance" if cd.imp < st.cfg.prompt_min_importance else None
    _add(st, cd, out, now, why)


def _add(st: State, cd: Cand, out: list, now: float, why: Optional[str] = None) -> None:
    cd.score = _score(st, cd, now)
    if why is None:
        if cd.key in st.vetoed or (cd.claim_id and cd.claim_id in st.vetoed):
            why = "vetoed"
        elif cd.imp < st.cfg.min_importance:
            why = "low_importance"
        elif cd.key in st.spoken or (cd.verdict is not None and cd.claim_id in st.voiced):
            why = "duplicate"             # one verdict-based line per claim: never correct, then confirm
    if why:
        _log(st, out, cd, "skipped", why)
        return
    st.pending[cd.key] = cd
    _log(st, out, cd, "candidate", "new", _text(st, cd, now))   # text lets D pre-synthesise TTS


def _tts(st: State, speaking: bool, now: float) -> None:
    c = st.cfg
    st.tts_until = max(st.tts_until, now + c.tts_cap_s) if speaking else now + c.post_tts_s


def _on_control(st: State, ev: Control, out: list, now: float) -> None:
    f, v = str(ev.flag or "").strip().lower(), ev.value
    if f == "mute":
        st.muted = truthy(v)
    elif f == "director":                         # director off: silent like mute (still logs what it would say)
        st.director_off = not truthy(v)
    elif f == "agent_speaking":
        _tts(st, truthy(v), now)
    elif f == "force_next":
        st.force_until = now + st.cfg.force_window_s if truthy(v) else NEG
    elif f == "aggr":
        st.aggr = clamp_aggr(v, st.aggr)
        st.cfg = level_cfg(st.aggr, st.cfg)
    elif f == "veto":
        _veto(st, v, out, now)
    elif f == "time":
        spk = _resolve_speaker(st, v)
        if spk is not None:
            _add(st, Cand(f"time:{spk}:{now:.0f}", "time", spk, side_of(st, spk), 1.0, 1.0, now), out, now)
            st.force_until = now + st.cfg.force_window_s


def _veto(st: State, v: Any, out: list, now: float) -> None:
    """value = decision key or claim id; True (no value) vetoes whatever would be said next."""
    if isinstance(v, bool) or v is None:
        ranked = sorted(st.pending.values(), key=lambda cd: _rank(st, cd, _score(st, cd, now)))
        if not (v and ranked):
            return
        v = ranked[0].key
    v = str(v)
    st.vetoed.add(v)
    for k, cd in list(st.pending.items()):
        if v in (k, cd.claim_id):
            _log(st, out, st.pending.pop(k), "dropped", "veto")


def _resolve_speaker(st: State, v: Any) -> Optional[str]:
    """Control(time) value: a speaker id, or a side ("prop"/"opp"), or True = whoever spoke last."""
    if isinstance(v, str) and v.strip():
        v = v.strip()
        if v in st.sides or v in st.last_speech:
            return v
        return next((s for s, side in st.sides.items() if side == v), v)
    if v is True and st.last_speech:
        return max(st.last_speech, key=lambda s: (st.last_speech[s], s))
    return None


# ----------------------------------------------------------------------------- firing
def try_fire(st: State, now: float) -> list[Event]:
    out: list[Event] = []
    for k, cd in list(st.pending.items()):                                   # G0
        if now - cd.t0 > TTL_S[cd.kind]:
            _log(st, out, st.pending.pop(k), "dropped", "stale_ttl")         # dashboard-only from now on
    if not st.pending or now < st.tts_until or not floor_open(st, now):      # G1, G2
        return out
    last = st.fires[-1][0] if st.fires else NEG
    if _latest_pause(st, now) <= last:                                       # G3: a human must pause again
        return out
    forced = now < st.force_until
    for cd in st.pending.values():
        cd.score = _score(st, cd, now)
    for cd in sorted(st.pending.values(), key=lambda cd: _rank(st, cd)):
        why = None if forced else _gate(st, cd, now, last)
        if why is None and (st.muted or st.director_off):
            why = "muted_would_fire"
        if why is None:
            text = _text(st, cd, now)
            _log(st, out, cd, "fired", "forced" if forced else "ok", text)
            return out + [_fire(st, cd, now, text)]
        _log(st, out, cd, "held", why, _text(st, cd, now) if why == "muted_would_fire" else "")
        if why == "muted_would_fire":
            break
    return out


def _gate(st: State, cd: Cand, now: float, last: float) -> Optional[str]:
    c, gap = st.cfg, now - last
    if cd.score < c.fire_threshold:                                          # G4
        return "below_threshold"
    if gap < c.hard_gap_s or (gap < c.min_gap_s and cd.score < c.override_score):   # G5
        return "cooldown"
    if sum(now - t < c.budget_window_s for t, *_ in st.fires) >= c.budget_n:      # G6
        return "budget"
    if cd.kind in PRESSURE and _streak(st, cd.side) >= c.max_streak and cd.score < c.streak_override:   # G7
        return "balance_streak"
    return None


def _score(st: State, cd: Cand, now: float) -> float:
    c = st.cfg
    recency = 0.5 ** (max(0.0, now - cd.t0) / (c.half_life_s * HL_MULT.get(cd.kind, 1.0)))
    boost = 1.0
    if cd.kind == "confirm" and \
            _side_pressure(st, cd.side) - _side_pressure(st, cd.side, same=False) >= c.confirm_boost_gap:
        boost = c.confirm_boost                   # an accurate claim by the over-pressured side gets voiced
    return KIND_W[cd.kind] * cd.conf * cd.imp * recency * boost


def _rank(st: State, cd: Cand, score: Optional[float] = None) -> tuple:
    """Deterministic total order: score, kind, less-pressured side first, older first, key."""
    s = cd.score if score is None else score
    return (-round(s, 2), KINDS.index(cd.kind), _side_pressure(st, cd.side), cd.t0, cd.key)


def _streak(st: State, side: str) -> int:
    n = 0
    for _, _, s, k in reversed(st.fires):
        if k not in PRESSURE:
            continue
        if s != side:
            break
        n += 1
    return n


def _text(st: State, cd: Cand, now: float) -> str:
    return voice.render(cd.kind, claim=cd.claim, verdict=cd.verdict, side=spoken_name(st, cd.target),
                        age_s=now - cd.t0)


def _fire(st: State, cd: Cand, now: float, text: str) -> Interjection:
    c = st.cfg
    _spent(st, cd.key, cd.kind, cd.claim_id, cd.target, cd.side, now)
    st.tts_until = now + len(text.split()) / c.words_per_s + c.post_tts_s   # until D's TTSState says otherwise
    marks: dict = {}
    for src in (cd.claim, cd.verdict):           # latency trace copied forward stage to stage
        if src is not None:
            marks.update(src.marks)
    marks["pause"] = _latest_pause(st, now)
    return Interjection(id=f"i-{next(_IDS):04d}", text=text, priority=round(cd.score, 3), claim_id=cd.claim_id,
                        kind=cd.kind, target=cd.target, marks=marks,
                        evidence=list(cd.verdict.evidence[:1]) if cd.verdict else [])   # D's source card


def _spent(st: State, key: Optional[str], kind: str, cid: Optional[str], target: Optional[str], side: str,
           now: float) -> None:
    """Bookkeeping for a line that was spoken (live, or found in the bus history on a crash-restore)."""
    if key:
        st.pending.pop(key, None)
        st.spoken.add(key)
    if cid and kind in LABEL2KIND.values():      # a source request isn't a verdict line: a later verdict may speak
        st.voiced.add(cid)
    st.fires.append((now, target, side, kind))
    if kind in PRESSURE and target is not None:
        st.pressure[target] += 1
    st.force_until = NEG


def restore_fired(st: State, ij: Interjection) -> None:
    """Crash-restore: an Interjection already in the bus history was spoken. Silently mark it spoken, so it is
    never said twice and the cooldown/budget/balance count it. Call in history order, after its Claim/Verdict."""
    cid, kind, now = ij.claim_id, ij.kind, ij.ts
    cl = st.claims.get(cid) if cid else None
    key = (f"prompt:{cid}" if kind == "prompt" else f"{kind}:{(cl.canonical_key if cl else None) or cid}") \
        if cid else None
    for k, cd in list(st.pending.items()):
        if cd.kind == kind and cd.claim_id == cid and (cid or cd.target == ij.target):
            st.spoken.add(k)
            st.pending.pop(k)
        elif cid and cd.claim_id == cid and cd.verdict is not None and kind in LABEL2KIND.values():
            st.pending.pop(k)                    # one verdict line per claim
    side = side_of(st, ij.target) if ij.target is not None else ""
    _spent(st, key, kind, cid, ij.target, side, now)


def _log(st: State, out: list, cd: Cand, action: str, reason: str, text: str = "") -> None:
    if st.last_log.get(cd.key) == (action, reason):
        return                                    # log transitions, not every tick
    st.last_log[cd.key] = (action, reason)
    extra = {"text": text} if text else {}
    out.append(DirectorDecision(key=cd.key, kind=cd.kind, claim_id=cd.claim_id, action=action, reason=reason,
                                score=round(cd.score, 3), target=cd.target, **extra))


def status(st: State, now: float) -> DirectorStatus:
    last = st.fires[-1][0] if st.fires else None
    ranked = sorted(((cd, _score(st, cd, now)) for cd in st.pending.values()), key=lambda x: _rank(st, *x))
    fl, busy, muted = floor_open(st, now), now < st.tts_until, st.muted or st.director_off
    blocked = ("nothing_pending" if not ranked else "own_tts" if busy else "floor_closed" if not fl else
               "awaiting_pause" if _latest_pause(st, now) <= (last if last is not None else NEG) else
               "muted" if muted else "")
    return DirectorStatus(
        floor_open=fl, tts_busy=busy, muted=muted, aggr=st.aggr,
        cooldown_left_s=round(max(0.0, st.cfg.min_gap_s - (now - last)), 1) if last is not None else 0.0,
        pending=[{"key": cd.key, "kind": cd.kind, "score": round(s, 3), "target": cd.target} for cd, s in ranked[:3]],
        pressure={s: st.pressure.get(s, 0) for s in [*st.sides, *st.pressure]}, blocked=blocked)


# ----------------------------------------------------------------------------- wrapper used by brain/run.py
class Director:
    """Holds one session's State and routes events into the pure core above."""

    def __init__(self, aggr: int = 3, side_names: Optional[dict] = None, cfg: Optional[Cfg] = None):
        self.side_names = dict(side_names or {})   # operator overrides: speaker id or side -> spoken name
        self._custom_cfg = cfg
        a = clamp_aggr(aggr)
        flags = getattr(config, "FLAGS", {}) or {}  # a crash-restart keeps the operator's mute
        self.st = State(cfg=cfg or level_cfg(a), aggr=a, muted=truthy(flags.get("mute", False)),
                        director_off=not truthy(flags.get("director", True)))
        self.reset()

    @property
    def aggr(self) -> int:
        return self.st.aggr

    def reset(self, session: Optional[SessionStart] = None) -> None:
        """Fresh session state. Operator settings (aggr, mute, side_names, custom cfg) carry over."""
        old = self.st
        cfg = old.cfg if self._custom_cfg is not None else level_cfg(old.aggr)   # re-reads config/env
        self.st = st = State(cfg=cfg, aggr=old.aggr, muted=old.muted, director_off=old.director_off,
                             overrides=self.side_names)
        if session is not None:
            # SpeakerInfo.side defaults to "prop": only a side the sender actually set counts. Declared sides
            # go first, so a speaker without one takes the side still free (names only -> prop, opp).
            for spk, info in session.speakers.items():
                if "side" in info.model_fields_set and norm_side(info.side):
                    st.sides[spk] = norm_side(info.side)
            for spk, info in session.speakers.items():
                side_of(st, spk)
                if info.name:
                    st.speaker_names[spk] = info.name

    def handle(self, ev: Union[Event, dict, str, bytes], now: float) -> list[Event]:
        if not isinstance(ev, Event):
            ev = parse_event(ev)
        if isinstance(ev, SessionStart):
            self.reset(ev)
            return []
        return on_event(self.st, ev, now)

    def notice(self, n: Notice, now: float) -> list[Event]:
        return on_notice(self.st, n, now)

    def tick(self, now: float) -> list[Event]:
        return try_fire(self.st, now)

    def restore_fired(self, ij: Interjection) -> None:
        restore_fired(self.st, ij)

    def status(self, now: float) -> DirectorStatus:
        return status(self.st, now)

    def name(self, speaker: str) -> str:
        """Spoken name for a speaker id ("Proposition", an operator override, or the id)."""
        return spoken_name(self.st, speaker)
