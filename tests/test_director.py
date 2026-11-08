"""Director + voice scenarios (design-director §7, ported to typed schemas events).

Every input is a real schemas event with an explicit `now`. Continuous speech is modelled the way STT
delivers it: an interim Utterance about every second (see talk()), because a speaker silent for 2.5 s
counts as paused (floor timeout). Speakers "A"/"B" get sides by first appearance unless a SessionStart says.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from app import config
from app.brain import director, voice
from app.brain.director import Cfg, Director
from app.brain.types import Notice
from app.schemas import (Claim, Control, DirectorDecision, DirectorStatus, Event, Evidence, Interjection, Numeric,
                         Pause, SessionStart, SpeakerInfo, TTSState, Utterance, Verdict, parse_event)

ROOT = Path(__file__).resolve().parents[1]
EVID = [Evidence(url="https://www.ons.gov.uk/x", title="Labour market", quote="q")]
TICK = "tick"
WORD = re.compile(r"[a-z]+")


@pytest.fixture(autouse=True)
def _default_flags(monkeypatch):
    """Director() reads the operator's mute from config.FLAGS; never inherit another test's flags."""
    monkeypatch.setattr(config, "FLAGS", {"director": True})
    monkeypatch.setattr(config, "DIRECTOR_DRY_FIRST_S", 0.0)   # the scenarios predate the dry line: see test_dry_*


# ----------------------------------------------------------------------------- builders
def C(cid, spk, t_end, imp=.8, typ="statistic", canon=None, topic="", **kw):
    return Claim(id=cid, speaker=spk, text=f"claim {cid}", type=typ, importance=imp, canonical_key=canon,
                 topic=topic, t_end=t_end, **kw)


def V(cid, label, conf, ev=True, **kw):
    return Verdict(claim_id=cid, label=label, confidence=conf, evidence=EVID if ev else [], **kw)


def P(spk, t):
    return Pause(speaker=spk, t=t)


def N(kind, spk, t, cid=None, **detail):
    return Notice(kind=kind, speaker=spk, t=t, claim_id=cid, detail=detail)


def talk(spk, a, b, step=1.0):
    """One speech segment from a to b: an interim Utterance every `step` s, all sharing t_start=a."""
    return [(a + i * step, Utterance(speaker=spk, text="words", t_start=a, t_end=a + i * step, final=False))
            for i in range(int((b - a) / step + 1e-9) + 1)]


def ticks(a, b, step=.25):
    return [(a + i * step, TICK) for i in range(int((b - a) / step + 1e-9) + 1)]


def run(events, d=None):
    """Feed (now, event) pairs in time order (stable for equal times). Every output must be a valid,
    wire-round-trippable schemas event, and every spoken line <= 15 words with no judgement words."""
    d = d or Director()
    outs = []
    for now, ev in sorted(events, key=lambda x: x[0]):
        o = d.tick(now) if isinstance(ev, str) else d.notice(ev, now) if isinstance(ev, Notice) else d.handle(ev, now)
        for x in o:
            assert isinstance(x, (Interjection, DirectorDecision))
            assert parse_event(x.model_dump_json()).model_dump() == x.model_dump()
            if isinstance(x, Interjection):
                assert len(x.text.split()) <= voice.MAX_WORDS and not voice.BANNED & set(WORD.findall(x.text.lower()))
        outs += [(now, x) for x in o]
    return d, outs


def fired(outs):
    return [(round(n, 2), x.kind, x.target, x.claim_id) for n, x in outs if isinstance(x, Interjection)]


def lines(outs):
    return [x for _, x in outs if isinstance(x, Interjection)]


def logs(outs, reason=None):
    return [(x.key, x.action, x.reason) for _, x in outs
            if isinstance(x, DirectorDecision) and (reason is None or x.reason == reason)]


def two_A_fired():
    """Two corrections against A, fired at 6 and 42; A keeps talking until 79."""
    return (talk("A", 0, 5) + [(4, C("a1", "A", 3)), (5, V("a1", "contradicted", .9)), (6, P("A", 6))]
            + talk("A", 7, 41) + [(40, C("a2", "A", 39)), (41, V("a2", "contradicted", .9)), (42, P("A", 42))]
            + talk("A", 43, 79))


def swap(events, m=None):
    m = m or {"A": "B", "B": "A"}
    out = []
    for n, e in events:
        if isinstance(e, Notice):
            e = replace(e, speaker=m.get(e.speaker, e.speaker))
        elif isinstance(e, Event) and hasattr(e, "speaker"):
            e = e.model_copy(update={"speaker": m.get(e.speaker, e.speaker)})
        out.append((n, e))
    return out


# ----------------------------------------------------------------------------- the 28 design scenarios
def test_01_fires_at_pause():
    _, o = run(talk("A", 0, 13) + [(10, C("c1", "A", 5)), (12, V("c1", "contradicted", .9)), (14, P("A", 14))])
    assert fired(o) == [(14, "correction", "A", "c1")]


def test_02_no_fire_while_speaking_then_fires_at_pause():
    ev = talk("A", 0, 10) + talk("A", 11, 14) + [(6, C("c1", "A", 5)), (8, V("c1", "contradicted", .9)),
                                                 (9, TICK), (15, P("A", 15))]
    _, o = run(ev)
    assert fired(o) == [(15, "correction", "A", "c1")]


def test_03_late_verdict_on_open_floor_fires_on_verdict():
    _, o = run(talk("A", 0, 6) + [(6, C("c1", "A", 5)), (7, P("A", 7)), (9, V("c1", "contradicted", .9))])
    assert fired(o) == [(9, "correction", "A", "c1")]


def test_04_other_speaker_took_floor_holds():
    ev = talk("A", 0, 6) + [(6, C("c1", "A", 5)), (7, P("A", 7))] + talk("B", 8, 12) + \
        [(9, V("c1", "contradicted", .9))] + ticks(9, 12)
    _, o = run(ev)
    assert fired(o) == []


def test_05_unverifiable_is_source_request_never_correction():
    _, o = run(talk("A", 0, 7) + [(6, C("c1", "A", 5, imp=.95)), (7, V("c1", "unverifiable", .99)), (8, P("A", 8))])
    assert [f[1] for f in fired(o)] == ["source_req"]
    assert not any(k.startswith("correction") for k, _, _ in logs(o))


def test_05b_minor_unverifiable_is_silent():
    _, o = run(talk("A", 0, 7) + [(6, C("c1", "A", 5, imp=.6)), (7, V("c1", "unverifiable", .99)), (8, P("A", 8))])
    assert fired(o) == [] and ("source_req:c1", "skipped", "unverifiable_minor") in logs(o)


def test_06_low_conf_skipped():
    _, o = run(talk("A", 0, 7) + [(6, C("c1", "A", 5)), (7, V("c1", "contradicted", .6)), (8, P("A", 8))])
    assert fired(o) == [] and ("correction:c1", "skipped", "low_conf") in logs(o)


def test_06b_evidence_capped_verdict_never_spoken_at_any_aggr():
    """C caps a verdict at 0.74 to keep it off air; aggr 4-5 lower min_conf below that, so the note itself decides."""
    for aggr in (3, 5):
        ev = talk("A", 0, 7) + [(6, C("c1", "A", 5)), (7, V("c1", "contradicted", .74, guard_notes=["capped"])),
                                (8, P("A", 8))]
        _, o = run(ev, Director(aggr=aggr))
        assert fired(o) == [] and ("correction:c1", "skipped", "capped") in logs(o)
    _, o = run(talk("A", 0, 7) + [(6, C("c1", "A", 5)), (7, V("c1", "contradicted", .74)), (8, P("A", 8))],
               Director(aggr=5))
    assert fired(o) == [(8, "correction", "A", "c1")]      # the same 0.74 without the note: aggr 5 voices it


def test_07_cooldown_holds_then_fires_after_gap():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("B", 7, 21) + [(20, C("c2", "B", 19)), (21, V("c2", "contradicted", .9)), (22, P("B", 22))]
          + talk("B", 23, 36) + [(37, P("B", 37))])
    _, o = run(ev)
    assert fired(o) == [(6, "correction", "A", "c1"), (37, "correction", "B", "c2")]
    assert ("correction:c2", "held", "cooldown") in logs(o)


def test_08_high_score_overrides_soft_cooldown():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("B", 7, 19) + [(19, C("c2", "B", 19, imp=1.0)), (19.5, V("c2", "contradicted", .97)), (20, P("B", 20))])
    _, o = run(ev)
    assert fired(o)[-1][:2] == (20, "correction")


def test_08b_never_inside_hard_gap():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("B", 7, 10) + [(10, C("c2", "B", 10, imp=1.0)), (10.5, V("c2", "contradicted", .99)), (11, P("B", 11))])
    _, o = run(ev + ticks(11, 17.75))
    assert len(fired(o)) == 1
    _, o = run(ev + ticks(11, 20))
    assert fired(o)[-1][0] == 18          # first moment the gap reaches hard_gap_s (12 s)


def test_09_stale_claim_dropped():
    _, o = run(talk("A", 0, 51) + [(2, C("c1", "A", 1)), (51, V("c1", "contradicted", .95)), (52, P("A", 52))])
    assert fired(o) == [] and ("correction:c1", "dropped", "stale_ttl") in logs(o)


def test_10_waits_for_own_tts():
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (5.5, TTSState(speaking=True)),
                            (6, P("A", 6)), (9, TTSState(speaking=False))] + ticks(9, 10.5)
    _, o = run(ev)
    assert fired(o)[0][0] == 9.5


def test_11_duplicate_canonical_claim_suppressed():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3, canon="unemp")), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("A", 7, 41) + [(40, C("c9", "A", 39, canon="unemp")), (41, V("c9", "contradicted", .9)),
                                (42, P("A", 42))])
    _, o = run(ev)
    assert len(fired(o)) == 1 and ("correction:unemp", "skipped", "duplicate") in logs(o)


STREAK_EV = two_A_fired() + [(78, C("a3", "A", 77)), (79, V("a3", "contradicted", .85)),
                             (79, C("b1", "B", 76, imp=.9)), (79.5, V("b1", "missing_context", .9)), (80, P("A", 80))]


def test_12_streak_defers_third_against_A_and_B_fires():
    _, o = run(STREAK_EV)
    assert fired(o)[-1][1:3] == ("context", "B") and ("correction:a3", "held", "balance_streak") in logs(o)


def test_13_strong_third_correction_still_fires():
    _, o = run(two_A_fired() + [(78, C("a3", "A", 78, imp=1.0)), (79, V("a3", "contradicted", .95)), (80, P("A", 80))])
    assert fired(o)[-1][1:4] == ("correction", "A", "a3")


def test_13b_weak_third_and_nothing_for_B_stays_silent():
    _, o = run(two_A_fired() + [(78, C("a3", "A", 77)), (79, V("a3", "contradicted", .85)), (80, P("A", 80))])
    assert len(fired(o)) == 2


def test_14_mute_logs_would_fire_and_unmute_fires():
    ev = talk("A", 0, 5) + [(1, Control(flag="mute", value=True)), (4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)),
                            (6, P("A", 6)), (8, Control(flag="mute", value=False))]
    _, o = run(ev)
    assert ("correction:c1", "held", "muted_would_fire") in logs(o) and fired(o) == [(8, "correction", "A", "c1")]
    held = [x for _, x in o if isinstance(x, DirectorDecision) and x.reason == "muted_would_fire"][0]
    assert held.text.startswith("Point of fact")          # rehearsal mode shows what it would have said


def test_15_force_waits_for_floor_then_skips_threshold_and_cooldown():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("B", 7, 12) + [(10, C("c2", "B", 9, imp=.5)), (10.5, V("c2", "missing_context", .8)),
                                (11, Control(flag="force_next")), (12, TICK), (13, P("B", 13))])
    _, o = run(ev)
    assert fired(o)[-1][:2] == (13, "context") and not any(n == 12 for n, *_ in fired(o))
    assert ("context:c2", "fired", "forced") in logs(o)


def test_16_superseded_verdict_no_correction():
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .8)), (6, V("c1", "supported", .7)),
                            (7, P("A", 7))]
    _, o = run(ev)
    assert fired(o) == [] and ("correction:c1", "dropped", "superseded") in logs(o)


def test_17_no_source_never_a_correction():
    # the schema itself downgrades a sourceless verdict to unverifiable ...
    _, o = run(talk("A", 0, 5) + [(4, C("c1", "A", 3, imp=.7)), (5, V("c1", "contradicted", .95, ev=False)),
                                  (6, P("A", 6))])
    assert fired(o) == [] and ("source_req:c1", "skipped", "unverifiable_minor") in logs(o)
    # ... and the Director still refuses one that bypassed validation
    raw = Verdict.model_construct(claim_id="c1", label="contradicted", confidence=.95, evidence=[])
    _, o = run(talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, raw), (6, P("A", 6))])
    assert fired(o) == [] and ("correction:c1", "skipped", "no_source") in logs(o)


def test_18_speech_closes_floor_and_force_still_waits_for_a_pause():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("A", 7, 20) + [(7.5, C("c2", "A", 7, imp=1.0)), (7.6, V("c2", "contradicted", .99)),
                                (8, Control(flag="force_next"))] + ticks(8, 20))
    _, o = run(ev)
    assert len(fired(o)) == 1
    _, o = run(ev + [(20.5, P("A", 20.5))])
    assert fired(o)[-1] == (20.5, "correction", "A", "c2") and ("correction:c2", "fired", "forced") in logs(o)


def test_19_tie_break_prefers_less_pressured_side():
    ev = (talk("A", 0, 5) + talk("B", 0, 5) + [(4, C("a1", "A", 3)), (5, V("a1", "contradicted", .9)),
                                               (6, P("A", 6)), (6, P("B", 6))]
          + talk("A", 7, 41) + [(40, C("a2", "A", 39)), (40, C("b2", "B", 39)), (41, V("a2", "contradicted", .9)),
                                (41, V("b2", "contradicted", .9)), (42, P("A", 42))])
    _, o = run(ev)
    assert fired(o)[-1][2] == "B"


def test_20_deterministic_replay():
    strip = lambda o: [(n, x.model_dump(exclude={"id"})) for n, x in o]   # noqa: E731
    _, o1 = run(STREAK_EV)
    _, o2 = run(STREAK_EV)
    assert strip(o1) == strip(o2) and len(o1) > 0
    ids = [x.id for x in lines(o1) + lines(o2)]
    assert len(set(ids)) == len(ids) and all(re.fullmatch(r"i-\d{4,}", i) for i in ids)   # global counter


def test_21_budget_caps():
    ev = []
    for i in range(3):
        t = 10 * i
        ev += talk("A", t, t + 3) + [(t + 2, C(f"c{i}", "A" if i % 2 == 0 else "B", t + 1)),
                                     (t + 3, V(f"c{i}", "contradicted", .95)), (t + 4, P("A", t + 4))]
    _, o = run(ev, Director(cfg=Cfg(budget_n=2, min_gap_s=0, hard_gap_s=0)))
    assert len(fired(o)) == 2 and any(r == "budget" for _, _, r in logs(o))


UNANSWERED_EV = talk("A", 0, 2) + [(2, C("a1", "A", 1, imp=.9))] + talk("B", 3, 30) + \
    [(30, N("unanswered", "B", 30, "a1"))]


def test_22_unanswered_prompt_goes_to_the_other_side():
    _, o = run(UNANSWERED_EV + [(31, P("B", 31)), (31, P("A", 31))])
    assert fired(o) == [(31, "prompt", "B", "a1")]
    assert lines(o)[0].text == "Opposition, your opponent's last point is still unanswered."


def test_23_side_symmetry():
    _, o1 = run(STREAK_EV)
    _, o2 = run(swap(STREAK_EV))
    m = {"A": "B", "B": "A"}
    assert [(n, k, m[s], c) for n, k, s, c in fired(o1)] == fired(o2)
    assert [x.text for x in lines(o1)] == [x.text for x in lines(o2)]


def test_24_confirm_boost_for_over_pressured_side():
    _, o = run(two_A_fired() + [(78, C("a3", "A", 77, imp=.9)), (79, V("a3", "supported", .9)), (80, P("A", 80))])
    assert fired(o)[-1][1:3] == ("confirm", "A")
    assert lines(o)[-1].text == "For the record, that checks out. Source: the ONS."


def test_24b_confirm_without_imbalance_is_dashboard_only():
    _, o = run(talk("A", 0, 5) + [(4, C("a3", "A", 3, imp=.9)), (5, V("a3", "supported", .9)), (6, P("A", 6))])
    assert fired(o) == []


# ----------------------------------------------------------------------------- floor timeout, notices
def test_floor_timeout_fires_without_a_pause():
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9))] + ticks(5, 9)
    _, o = run(ev)
    assert fired(o) == [(7.5, "correction", "A", "c1")]       # 2.5 s after A was last heard


def test_floor_timeout_never_monologues_into_silence():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9))] + ticks(5, 30)
          + [(8, C("c2", "A", 4, imp=1.0, canon="other")), (8.5, V("c2", "contradicted", .99))])
    _, o = run(ev)
    assert len(fired(o)) == 1                                 # needs a fresh human pause/timeout first


def test_stale_pause_cannot_open_the_floor():
    # A resumes at 6.6; the VAD Pause stamped 6.8 is stale whichever of the two arrives first
    base = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (7.3, V("c1", "contradicted", .9))]
    resume = (7.0, Utterance(speaker="A", text="and", t_start=6.6, t_end=7.0, final=False))
    for order in ([(6.8, P("A", 6.8)), resume], [resume, (7.2, P("A", 6.8))]):
        _, o = run(base + order + ticks(7.5, 9.25))
        assert fired(o) == []
        _, o = run(base + order + ticks(7.5, 10))
        assert fired(o) == [(9.5, "correction", "A", "c1")]    # floor timeout: 2.5 s after A was last heard


def test_late_final_of_earlier_speech_does_not_close_the_floor():
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5.8, P("A", 5.8)),
                            (6.0, Utterance(speaker="A", text="final", t_start=1.0, t_end=5.0, final=True)),
                            (6.2, V("c1", "contradicted", .9))]
    _, o = run(ev)
    assert fired(o) == [(6.2, "correction", "A", "c1")]


def test_one_verdict_line_per_claim():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3, imp=1.0)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("A", 7, 40) + [(30, V("c1", "supported", .95)), (41, P("A", 41))])
    _, o = run(ev, Director(aggr=5))
    assert fired(o) == [(6, "correction", "A", "c1")] and ("confirm:c1", "skipped", "duplicate") in logs(o)


def test_answered_notice_drops_pending_prompt():
    _, o = run(UNANSWERED_EV + [(30.5, N("answered", "B", 30.5, "a1")), (31, P("B", 31)), (31, P("A", 31))])
    assert fired(o) == [] and ("prompt:a1", "dropped", "answered") in logs(o)


def test_unanswered_needs_known_important_claim():
    ev = talk("A", 0, 2) + [(2, C("a1", "A", 1, imp=.5))] + talk("B", 3, 30) + \
        [(30, N("unanswered", "B", 30, "a1")), (30, N("unanswered", "B", 30, "zz")), (31, P("B", 31))]
    _, o = run(ev)
    assert fired(o) == []
    assert ("prompt:a1", "skipped", "low_importance") in logs(o) and ("prompt:zz", "skipped", "unknown_claim") in logs(o)


def test_nonresponsive_is_dashboard_only_by_default(monkeypatch):
    monkeypatch.delattr(config, "DIRECTOR_VOICE_NONRESPONSIVE", raising=False)
    ev = talk("A", 0, 2) + [(2, C("a1", "A", 1, imp=1.0))] + talk("B", 3, 30) + \
        [(30, N("nonresponsive", "B", 30, None, ignored=["a1"], turn=2)), (31, P("B", 31))]
    _, o = run(ev)
    assert fired(o) == [] and ("nonresponsive:B:2", "skipped", "dashboard_only") in logs(o)
    monkeypatch.setattr(config, "DIRECTOR_VOICE_NONRESPONSIVE", True, raising=False)
    _, o = run(ev)
    assert fired(o) == [(31, "prompt", "B", "a1")]


# ----------------------------------------------------------------------------- opaque speaker ids + sides
def test_opaque_speaker_ids_with_session_sides():
    d = Director()
    assert d.handle(SessionStart(speakers={"spk1": SpeakerInfo(name="Priya", side="opp"),
                                           "spk2": SpeakerInfo(name="Tom", side="prop")}), 0) == []
    ev = (talk("spk2", 0, 5) + [(4, C("c1", "spk2", 3)), (4, C("c2", "spk2", 3, imp=1.0, topic="bus use outside London")),
                                (5, V("c1", "contradicted", .9)), (6, P("spk2", 6))]
          + talk("spk1", 7, 40) + [(40, N("unanswered", "spk1", 40, "c2")), (41, P("spk1", 41))])
    d, o = run(ev, d)
    assert fired(o) == [(6, "correction", "spk2", "c1"), (41, "prompt", "spk1", "c2")]
    assert lines(o)[1].text == "Opposition, you haven't yet answered the point on bus use outside London."
    assert d.name("spk2") == "Proposition" and d.name("spk1") == "Opposition"
    assert d.status(41).pressure == {"spk1": 1, "spk2": 1}


def test_sides_without_session_start_follow_first_appearance():
    d, _ = run(talk("Gov", 0, 2) + talk("Opp", 3, 5) + talk("X", 6, 7))
    assert [d.name(s) for s in ("Gov", "Opp", "X")] == ["Proposition", "Opposition", "Proposition"]
    d, _ = run(talk("Gov", 0, 2) + talk("Opp", 3, 5), Director(side_names={"prop": "Government", "Opp": "the Opposition"}))
    assert (d.name("Gov"), d.name("Opp"), d.name("never_seen")) == ("Government", "the Opposition", "never_seen")


def test_session_start_resets_state_but_keeps_operator_settings():
    d, _ = run(two_A_fired() + [(0, Control(flag="aggr", value=4)), (0, Control(flag="mute", value=False))])
    d.handle(SessionStart(speakers={"B": SpeakerInfo(side="prop"), "A": SpeakerInfo(side="opp")}), 100)
    s = d.status(100)
    assert s.pressure == {"B": 0, "A": 0} and s.aggr == 4 and s.pending == [] and d.name("A") == "Opposition"


# ----------------------------------------------------------------------------- controls, tts, outputs
def test_controls_director_off_aggr_unknown_flag():
    d = Director()
    assert d.handle(Control(flag="no_such_flag", value=1), 0) == []
    d.handle(Control(flag="aggr", value=9), 0)
    assert d.aggr == 5 and d.st.cfg.min_conf == .65
    d.handle(Control(flag="aggr", value="2"), 0)
    assert d.aggr == 2 and d.st.cfg.min_gap_s == 45
    d.handle(Control(flag="aggr", value="up"), 0)
    assert d.aggr == 3
    _, o = run(talk("A", 0, 5) + [(1, Control(flag="director", value=False)), (4, C("c1", "A", 3)),
                                  (5, V("c1", "contradicted", .9)), (6, P("A", 6))])
    assert fired(o) == [] and ("correction:c1", "held", "muted_would_fire") in logs(o)


def test_veto_by_key_and_by_claim_id():
    base = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9))]
    _, o = run(base + [(5.5, Control(flag="veto", value="correction:c1")), (6, P("A", 6))])
    assert fired(o) == [] and ("correction:c1", "dropped", "veto") in logs(o)
    _, o = run(talk("A", 0, 5) + [(1, Control(flag="veto", value="c1")), (4, C("c1", "A", 3)),
                                  (5, V("c1", "contradicted", .9)), (6, P("A", 6))])
    assert fired(o) == [] and ("correction:c1", "skipped", "vetoed") in logs(o)


def test_time_control_addresses_named_speaker():
    _, o = run(talk("A", 0, 2) + talk("B", 3, 9) + [(8, Control(flag="time", value="B")), (10, P("B", 10))])
    assert [f[1:3] for f in fired(o)] == [("time", "B")]
    assert lines(o)[0].text == "Time, Opposition. Please wrap up." and lines(o)[0].claim_id is None


def test_tts_safety_cap_and_agent_speaking_control():
    base = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
    _, o = run(base + [(5.5, TTSState(speaking=True))] + ticks(6, 15))
    assert fired(o)[0][0] == 13.5                              # speaking=True never followed by False: 8 s cap
    _, o = run(base + [(5.5, Control(flag="agent_speaking", value=True)),
                       (7, Control(flag="agent_speaking", value=False))] + ticks(6, 9))
    assert fired(o)[0][0] == 7.5


def test_interjection_fields():
    num = Numeric(claimed=6, actual=4.1, unit="%")
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 3, marks={"speech_end": 3.0, "claim": 3.4})),
                            (5, V("c1", "contradicted", .9, numeric=num, marks={"verdict": 4.9})), (6, P("A", 6))]
    _, o = run(ev)
    ij = lines(o)[0]
    assert ij.text == "Point of fact: the ONS puts that at 4.1 percent, not 6 percent."
    assert ij.kind == "correction" and ij.target == "A" and ij.claim_id == "c1"
    assert ij.evidence[0].url == EVID[0].url and len(ij.evidence) == 1
    assert 0 < ij.priority <= 1 and ij.marks == {"speech_end": 3.0, "claim": 3.4, "verdict": 4.9, "pause": 6}
    cand = [x for _, x in o if isinstance(x, DirectorDecision) and x.action == "candidate"][0]
    assert cand.text == ij.text and cand.target == "A"         # pre-synthesis hint for D


def test_status_is_a_valid_director_status():
    d, _ = run(talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6)),
                                  (7, C("c2", "A", 6)), (7, V("c2", "missing_context", .9))])
    s = d.status(7)
    assert isinstance(s, DirectorStatus) and DirectorStatus.model_validate(s.model_dump()) == s
    assert s.floor_open and s.tts_busy and 0 < s.cooldown_left_s <= 30 and not s.muted and s.aggr == 3
    assert s.pending == [{"key": "context:c2", "kind": "context", "score": s.pending[0]["score"], "target": "A"}]
    assert s.pressure == {"A": 1}
    assert parse_event(s.model_dump_json()).model_dump() == s.model_dump()
    assert Director().status(0).blocked == "nothing_pending"


def test_restore_without_speech_never_fires():
    d = Director()
    out = d.handle(C("c1", "A", 3), 3) + d.handle(V("c1", "contradicted", .95), 4) + d.tick(5)
    assert not [x for x in out if isinstance(x, Interjection)]
    assert d.handle(V("zz", "contradicted", .95), 6) == []    # verdict for an unknown claim is ignored
    assert d.handle({"event": "Pause", "speaker": "A", "t": 7}, 7) == [] and d.handle(Event(event="Other"), 8) == []


def test_config_overrides_level_3_only(monkeypatch):
    monkeypatch.setattr(config, "DIRECTOR_MIN_GAP_S", 10.0)
    monkeypatch.setattr(config, "DIRECTOR_MIN_CONF", .5)
    assert Director(aggr=3).st.cfg.min_gap_s == 10 and Director(aggr=3).st.cfg.min_conf == .65
    assert Director(aggr=4).st.cfg.min_gap_s == 20


def test_fake_debate_fixture_end_to_end():
    """The drive-mode stream (mock ears: interims every 3 words) + one sourced contradiction."""
    from app.bus import load_jsonl
    from app.mocks.mock_ears import schedule
    ev = [(t, e) for t, e in schedule(load_jsonl(ROOT / "fixtures" / "fake_debate.jsonl"))]
    gov = [Evidence(url="https://www.gov.uk/air", title="Air pollution", quote="q")]
    ev += [(11.3, C("c1", "A", 10.12, imp=1.0, topic="air pollution deaths")),
           (13.0, Verdict(claim_id="c1", label="missing_context", confidence=.9, evidence=gov,
                          short_fact="the figure is a modelled estimate"))] + ticks(0, 40)
    d, o = run(ev)
    assert fired(o) == [(18.73, "context", "A", "c1")]          # at A's Pause, before B starts
    assert lines(o)[0].text == "Some context: GOV.UK notes the figure is a modelled estimate."
    assert d.name("B") == "Opposition"


# ----------------------------------------------------------------------------- dry-spell canned line
@pytest.fixture
def dry_on(monkeypatch):
    monkeypatch.setattr(config, "DIRECTOR_DRY_FIRST_S", 25.0)


def test_dry_first_line_at_first_pause_after_25s(dry_on):
    ev = talk("A", 0, 20) + [(21, P("A", 21))] + talk("A", 22, 29) + [(30, P("A", 30))] + ticks(30, 40)
    _, o = run(ev)
    assert fired(o) == [(30, "dry", None, None)]
    assert lines(o)[0].text == "Neither side has offered a figure yet. Care to name one?"


def test_dry_then_waits_for_the_gap_and_rewords_once_a_figure_is_heard(dry_on):
    ev = (talk("A", 0, 25) + [(26, P("A", 26))]                       # dry #1 at 26
          + talk("B", 31, 45) + [(40, C("b1", "B", 39)), (46, P("B", 46))]   # 20 s after: still cooling down
          + talk("B", 47, 56) + [(57, P("B", 57))] + ticks(57, 70))     # 31 s after: dry #2
    _, o = run(ev)
    assert fired(o) == [(26, "dry", None, None), (57, "dry", None, None)]
    assert lines(o)[1].text == "Either side, a figure I can check?"


def test_dry_ranks_below_a_real_line_and_resets_the_clock(dry_on):
    ev = talk("A", 0, 27) + [(20, C("c1", "A", 19)), (27, V("c1", "contradicted", .9)), (28, P("A", 28))] \
        + talk("B", 33, 50) + [(51, P("B", 51))] + ticks(51, 60)
    _, o = run(ev)
    # the dry line (due at 25) never jumps a real one, and the next is due 30 s after the correction
    assert fired(o) == [(28, "correction", "A", "c1"), (58, "dry", None, None)]


def test_dry_never_speaks_into_silence_or_when_muted(dry_on):
    _, o = run(talk("A", 0, 26) + [(27, P("A", 27))] + ticks(27, 200))
    assert fired(o) == [(27, "dry", None, None)]                     # no human pause after it: one line only
    ev = talk("A", 0, 26) + [(27, P("A", 27))] + talk("B", 32, 40) + [(41, P("B", 41))] + ticks(41, 120)
    _, o = run(ev)
    assert fired(o) == [(27, "dry", None, None)]                     # due at 57, but the room went quiet at 40
    assert ("dry:1", "held", "room_quiet") in logs(o)
    _, o = run([(0, Control(flag="mute", value=True))] + talk("A", 0, 26) + [(27, P("A", 27))])
    assert fired(o) == [] and ("dry:0", "held", "muted_would_fire") in logs(o)


# ----------------------------------------------------------------------------- voice
def test_every_template_renders_with_nothing():
    for kind in [*voice.T, "unknown_kind"]:
        for age in (0, 100):
            text = voice.render(kind, age_s=age)
            assert text and len(text.split()) <= voice.MAX_WORDS and voice.ok(text)
            text = voice.render(kind, claim=Claim(id="c", speaker="A", text="x"),
                                verdict=Verdict(claim_id="c"), side="", age_s=age)
            assert text and len(text.split()) <= voice.MAX_WORDS
    for tpls in voice.T.values():                              # last template needs at most src/side
        assert tpls[-1].format(**voice.FALLBACK)


def test_no_banned_words_in_any_template():
    for tpl in [t for tpls in voice.T.values() for t in tpls] + [voice.OPENER]:
        assert not voice.BANNED & set(WORD.findall(tpl.lower())), tpl


def test_voice_variants():
    v = Verdict(claim_id="c", label="contradicted", confidence=.9, evidence=EVID, short_fact="crime fell 3% last year")
    cl = Claim(id="c", speaker="A", text="x", topic="NHS waiting lists")
    assert voice.render("correction", cl, v) == "Point of fact: the ONS says crime fell 3% last year."
    assert voice.render("correction", cl, v, age_s=20) == "Earlier, on NHS waiting lists: the ONS says crime fell 3% last year."
    v2 = v.model_copy(update={"short_fact": "the claim is false", "evidence": [
        Evidence(url="https://example.org/r", title="Crime survey 2024 - Office for National Statistics")]})
    assert voice.render("correction", cl, v2) == "Point of fact: Office for National Statistics contradicts that. Details on screen."
    long = cl.model_copy(update={"topic": "the very long and winding topic that goes on and on"})
    assert voice.render("source_req", long, side="Proposition") == "Proposition, what's your source for that? I couldn't verify it."
    assert voice.render("prompt", side="Opposition") == "Opposition, your opponent's last point is still unanswered."
    assert voice.source_name([{"url": "https://data.ons.gov.uk/x"}]) == "the ONS"
    assert voice.source_name([{"url": "https://ghent.be/report", "title": ""}]) == "ghent.be"


def test_say_numbers():
    assert voice.say(4.1, "%") == "4.1 percent" and voice.say(7.6e6) == "7.6 million"
    assert voice.say(3e9, "GBP") == "£3 billion" and voice.say(40000, "deaths") == "40,000 deaths"
    assert voice.say(None) == "" and voice.say(-2.5, "%") == "-2.5 percent" and voice.say(1234567) == "1.23 million"


def test_judgement_words_in_short_fact_fall_back_to_next_template():
    cl = Claim(id="c", speaker="A", text="x")
    for sf in ("that figure is exaggerated", "the claim is inaccurate", "this is a myth", "it is a fabrication",
               "a misleading framing", "the number was falsified"):
        v = Verdict(claim_id="c", label="contradicted", confidence=.9, evidence=EVID, short_fact=sf)
        text = voice.render("correction", cl, v)
        assert text == "Point of fact: the ONS contradicts that. Details on screen.", sf
        assert not voice.ok(f"The ONS says {sf}.")
    assert voice.ok("Point of fact: the ONS says it is 4.1 percent.")


# ----------------------------------------------------------------------------- review regressions
def test_director_off_is_not_undone_by_unmute():
    base = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
    _, o = run(base + [(1, Control(flag="director", value=False)), (2, Control(flag="mute", value=False))])
    assert fired(o) == [] and ("correction:c1", "held", "muted_would_fire") in logs(o)
    d, o = run(base + [(1, Control(flag="director", value="false")), (7, Control(flag="director", value="true"))])
    assert fired(o) == [(7, "correction", "A", "c1")] and not d.status(8).muted
    d, _ = run([(1, Control(flag="director", value=False)), (2, Control(flag="aggr", value=4))])
    d.handle(SessionStart(), 3)
    assert d.status(3).muted                                   # operator settings survive a new session


def test_string_false_controls_are_false():
    base = talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
    _, o = run(base + [(5.5, Control(flag="agent_speaking", value="true")),
                       (7, Control(flag="agent_speaking", value="false"))] + ticks(6, 9))
    assert fired(o)[0][0] == 7.5
    _, o = run(base + [(1, Control(flag="mute", value="true")), (7, Control(flag="mute", value="false"))])
    assert fired(o) == [(7, "correction", "A", "c1")]
    _, o = run(base + [(5.5, parse_event({"event": "TTSState", "speaking": "true"})),
                       (7, parse_event({"event": "TTSState", "speaking": "false"}))] + ticks(6, 9))
    assert fired(o)[0][0] == 7.5


def finals(spk, a, n, seg=3.0, lat=.3):
    """Finals-only ears: continuous speech from a, one final per `seg` s of speech, arriving `lat` s late."""
    return [(a + seg * (i + 1) + lat, Utterance(speaker=spk, text="words", t_start=a + seg * i, t_end=a + seg * (i + 1)))
            for i in range(n)]


def test_finals_only_ears_never_open_the_floor_mid_monologue():
    ev = finals("A", 0, 7) + [(4, C("c1", "A", 3)), (4.5, V("c1", "contradicted", .9))] + ticks(0, 35)
    _, o = run(ev)
    assert fired(o) == [(27.5, "correction", "A", "c1")]      # first tick 6 s after the last final (21.3), never 3.3 + 2.5
    _, o = run(ev + [(21.8, P("A", 21.8))])
    assert fired(o) == [(21.8, "correction", "A", "c1")]      # a real Pause still opens the floor at once
    long = [(110.3, Utterance(speaker="A", text="w", t_start=100, t_end=110)), (111, C("c1", "A", 109)),
            (111.5, V("c1", "contradicted", .9))]
    _, o = run(long + ticks(110, 130))
    assert fired(o) == [(123, "correction", "A", "c1")]       # a 10 s final: the next may be 10 s away (+2.5)
    # once interims are seen, the usual 2.5 s timeout applies: test_floor_timeout_fires_without_a_pause


def test_stream_relative_and_wrong_clock_times_are_ignored():
    T = 1_762_532_400.0
    d = Director()
    d.handle(C("c1", "A", 2.8, imp=1.0), T)                   # stream-relative t_end: not "decades old"
    d.handle(Utterance(speaker="A", text="first point", t_start=10.0, t_end=12.0, ts=T), T)
    d.handle(Pause(speaker="A", ts=T + .5), T + .5)
    d.handle(Utterance(speaker="A", text="still talking", final=False, t_start=13.0, t_end=14.5, ts=T + 2), T + 2)
    out = d.handle(V("c1", "contradicted", .95), T + 2.1)
    assert not [x for x in out if isinstance(x, Interjection)]   # A is mid-sentence: floor closed
    out = d.handle(Pause(speaker="A", t=15.3, ts=T + 3), T + 3)  # relative Pause.t -> its ts
    assert [x.kind for x in out if isinstance(x, Interjection)] == ["correction"]


def test_verdict_before_its_claim_is_voiced_when_the_claim_arrives():
    _, o = run(talk("A", 0, 5) + [(4, V("c1", "contradicted", .9)), (4.5, C("c1", "A", 3)), (6, P("A", 6))])
    assert fired(o) == [(6, "correction", "A", "c1")]
    _, o = run(talk("A", 0, 51) + [(2, V("c1", "contradicted", .9)), (51, C("c1", "A", 1)), (52, P("A", 52))])
    assert fired(o) == []                                     # the claim's own TTL still applies


def test_resent_claim_upsert_re_evaluates_its_verdict():
    _, o = run(talk("A", 0, 5) + [(4, C("c1", "A", 3, imp=.2)), (4.5, V("c1", "contradicted", .9)),
                                  (5, C("c1", "A", 3, imp=.9)), (6, P("A", 6))])
    assert ("correction:c1", "skipped", "low_importance") in logs(o) and fired(o) == [(6, "correction", "A", "c1")]
    _, o = run(talk("A", 0, 5) + [(4, C("c1", "A", 3)), (4.5, V("c1", "contradicted", .9)),
                                  (5, C("c1", "A", 3, checkable=False)), (6, P("A", 6))])
    assert fired(o) == [] and ("correction:c1", "skipped", "not_checkable") in logs(o)
    _, o = run(talk("A", 0, 5) + [(4, C("c1", "A", 3)), (4.5, V("c1", "contradicted", .9)),
                                  (5, C("c1", "A", 3)), (6, P("A", 6))])
    assert fired(o) == [(6, "correction", "A", "c1")] and ("correction:c1", "dropped", "superseded") not in logs(o)


def test_source_request_then_web_verdict_still_corrects():
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3, imp=1.0)), (4.5, V("c1", "unverifiable", .3)), (6, P("A", 6))]
          + talk("A", 7, 20) + [(12, V("c1", "contradicted", .95))] + [(21, P("A", 21))] + ticks(21, 40))
    _, o = run(ev, Director(aggr=5))
    assert [f[1] for f in fired(o)] == ["source_req", "correction"]
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3, imp=1.0)), (4.5, V("c1", "contradicted", .95)), (6, P("A", 6))]
          + talk("A", 7, 20) + [(12, V("c1", "unverifiable", .3)), (21, P("A", 21))] + ticks(21, 40))
    _, o = run(ev, Director(aggr=5))
    assert [f[1] for f in fired(o)] == ["correction"] and ("source_req:c1", "skipped", "duplicate") in logs(o)


def test_session_sides_missing_or_aliased():
    d = Director()
    d.handle(SessionStart(speakers={"A": SpeakerInfo(name="Priya"), "B": SpeakerInfo(name="Tom")}), 0)
    assert (d.name("A"), d.name("B")) == ("Proposition", "Opposition")
    d.handle(SessionStart(speakers={"A": SpeakerInfo(name="Priya"), "B": SpeakerInfo(side="prop")}), 0)
    assert (d.name("A"), d.name("B")) == ("Opposition", "Proposition")   # the declared side wins
    d.handle(SessionStart(speakers={"A": {"side": "Opposition"}, "B": {"side": "government"}}), 0)
    assert (d.name("A"), d.name("B")) == ("Opposition", "Proposition")
    d.handle(SessionStart(speakers={"A": {"side": "opp"}}), 0)
    d.handle(C("x", "B", 1, side="neutral"), 1)                # unknown side hint: the free side
    assert d.name("B") == "Proposition"


def test_restore_fired_marks_spoken_counts_cooldown_and_keeps_one_line_per_claim():
    d2 = Director(cfg=Cfg(min_gap_s=0))
    for x in (C("c1", "A", 3), V("c1", "supported", .95)):     # history holds the LATEST verdict, before the line
        d2.handle(x, 5)
    d2.handle(Control(flag="time", value="A"), 6.5)
    d2.restore_fired(Interjection(id="i-1", text="Point of fact", kind="correction", claim_id="c1", target="A", ts=6))
    d2.restore_fired(Interjection(id="i-2", text="Time", kind="time", target="A", ts=7))
    assert d2.st.pending == {} and "correction:c1" in d2.st.spoken and "c1" in d2.st.voiced
    assert len(d2.st.fires) == 2 and d2.status(8).pressure == {"A": 2} and d2.st.force_until == float("-inf")
    d2, o = run(talk("A", 9, 12) + [(10, C("c2", "A", 9, imp=1.0)), (10.5, V("c2", "contradicted", .99)),
                                    (13, P("A", 13))] + ticks(13, 19.5), d2)
    assert fired(o) == [(19, "correction", "A", "c2")]        # the 12 s hard gap counts from the restored line
    _, o = run([(20, V("c1", "contradicted", .95)), (21, P("A", 21))], d2)
    assert fired(o) == [] and ("correction:c1", "skipped", "duplicate") in logs(o)


# ----------------------------------------------------------------------------- aggr 3 reachability of prompt/source_req
def test_prompt_at_importance_067_fires_at_aggr3_next_pause():
    # LLM importance 2 (0.67) is the documented floor for voiced prompts; the next pause can be ~10 s after the
    # argmap notices (it only notices once the next speaker's final arrives)
    ev = talk("A", 0, 2) + [(2, C("a1", "A", 1, imp=2))] + talk("B", 3, 30) + [(30, N("unanswered", "B", 30, "a1"))]
    _, o = run(ev + talk("B", 31, 39) + [(40, P("B", 40))])
    assert fired(o) == [(40, "prompt", "B", "a1")]
    _, o = run(ev + talk("B", 31, 49) + [(50, P("B", 50))])
    assert fired(o) == [] and ("prompt:a1", "held", "below_threshold") in logs(o)   # still decays
    for lv in (1, 2):                                          # quieter levels still suppress weak prompts
        _, o = run(ev + [(31, P("B", 31))], Director(aggr=lv))
        assert fired(o) == [] and ("prompt:a1", "held", "below_threshold") in logs(o)


def test_stronger_prompt_outranks_weaker_one():
    ev = talk("A", 0, 2) + [(2, C("a1", "A", 1, imp=2)), (2, C("a2", "A", 1.5, imp=3))] + talk("B", 3, 30) + \
        [(30, N("unanswered", "B", 30, "a1")), (30, N("unanswered", "B", 30, "a2")), (31, P("B", 31))]
    _, o = run(ev)
    assert fired(o) == [(31, "prompt", "B", "a2")]


def test_source_request_fires_when_verdict_lands_seconds_after_claim():
    ev = talk("A", 0, 12) + [(5.5, C("c1", "A", 5, imp=.8)), (8.5, V("c1", "unverifiable", .2))]
    _, o = run(ev + [(13, P("A", 13))])                        # verdict 3.5 s after the claim, pause 8 s after
    assert fired(o) == [(13, "source_req", "A", "c1")]
    assert lines(o)[0].text.startswith("Proposition, what's your source")


def test_prompt_and_source_request_still_respect_cooldown_and_rank_under_confident_verdicts():
    ev = (talk("A", 0, 2) + [(2, C("a1", "A", 1, imp=.8))] + talk("B", 3, 30)
          + [(29, C(c, "B", 29, imp=.8, canon=c)) for c in ("c1", "c2", "c3")]
          + [(30, V("c1", "contradicted", .9)), (30, V("c2", "missing_context", .9)), (30, V("c3", "unverifiable", .2)),
             (30, N("unanswered", "B", 30, "a1"))])
    d, _ = run(ev)
    st = d.st
    ranked = sorted(st.pending.values(), key=lambda cd: director._rank(st, cd, director._score(st, cd, 30)))
    assert [cd.kind for cd in ranked] == ["correction", "context", "prompt", "source_req"]
    strong = [director._score(st, replace(cd, imp=1.0, t0=30), 30) for cd in ranked if cd.kind in ("prompt", "source_req")]
    assert max(strong) < min(st.cfg.override_score, st.cfg.streak_override)   # can't jump cooldown or balance
    # a prompt detected just after a correction waits out the 30 s cooldown, then still fires
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + [(5, C("a1", "A", 4, imp=1.0, canon="x"))] + talk("B", 7, 10) + [(10, N("unanswered", "B", 10, "a1")),
                                                                             (11, P("B", 11))]
          + talk("B", 12, 36) + [(37, P("B", 37))])
    _, o = run(ev)
    assert fired(o) == [(6, "correction", "A", "c1"), (37, "prompt", "B", "a1")]
    assert ("prompt:a1", "held", "cooldown") in logs(o)


# ----------------------------------------------------------------------------- hardening regressions


def test_malformed_evidence_url_never_blocks_the_referee():
    """urlparse raises on an unmatched bracket: the bad candidate raised on every Pause/tick and held the queue."""
    assert voice.source_name([{"url": "https://[broken/x"}]) == ""
    assert voice.source_name([{"url": "http://a]b/", "title": "Crime survey - Office for National Statistics"}]) == \
        "Office for National Statistics"
    assert voice.source_name([{"url": "https://[broken/x"}, {"url": "https://www.ons.gov.uk/x"}]) == "the ONS"
    bad = [Evidence(url="https://[broken/x", title="", quote="q")]
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 3, imp=1.0)), (4.5, Verdict(claim_id="c1", label="contradicted",
                                                                        confidence=.95, evidence=bad)),
                            (4.6, C("c2", "A", 3, canon="k2")), (4.8, V("c2", "missing_context", .9)), (6, P("A", 6))]
    _, o = run(ev)
    assert fired(o) == [(6, "correction", "A", "c1")]
    assert lines(o)[0].text == "Point of fact: the source contradicts that. Details on screen."


def test_judgement_word_in_numeric_unit_is_never_spoken():
    v = {"evidence": [{"url": "https://www.ons.gov.uk/x"}], "numeric": {"claimed": 6, "actual": 4, "unit": "false claims"}}
    text = voice.render("correction", verdict=v)
    assert voice.ok(text) and text == "Point of fact: the ONS contradicts that. Details on screen."
    v["numeric"]["unit"] = "deaths"
    assert voice.render("correction", verdict=v) == "Point of fact: the ONS puts that at 4 deaths, not 6 deaths."


def test_a_pause_with_no_speech_since_the_last_line_never_reopens_the_floor():
    """The referee's own echo makes the bridge send Pause(A) with no new human speech: that is not a human pause."""
    ev = talk("A", 0, 5) + [(4, C("c1", "A", 4, imp=1.0)), (4, C("c2", "A", 4, imp=1.0, canon="k2")),
                            (5, V("c1", "contradicted", .95)), (5, V("c2", "contradicted", .95)), (6, P("A", 6)),
                            (14, P("A", 14))] + ticks(6, 48)
    d, o = run(ev)
    assert fired(o) == [(6, "correction", "A", "c1")]
    assert d.status(48).blocked == "awaiting_pause"
    _, o = run(ev + talk("A", 40, 41) + [(42, P("A", 42))])     # a human speaks and pauses: the next line may come
    assert fired(o) == [(6, "correction", "A", "c1"), (42, "correction", "A", "c2")]


def test_time_call_is_what_its_force_window_fires():
    """Control(time) forces the time line, not whichever candidate outscores it (the call was then dropped stale)."""
    ev = (talk("A", 0, 5) + [(4, C("c1", "A", 3)), (5, V("c1", "contradicted", .9)), (6, P("A", 6))]
          + talk("B", 7, 15) + [(14, C("c2", "B", 14, imp=1.0)), (14.5, V("c2", "contradicted", .99)),
                                (15, Control(flag="time", value="B")), (15.5, P("B", 15.5))] + ticks(15.5, 65))
    _, o = run(ev)
    assert fired(o)[:2] == [(6, "correction", "A", "c1"), (15.5, "time", "B", None)]
    assert ("time:B:15", "fired", "forced") in logs(o) and ("time:B:15", "dropped", "stale_ttl") not in logs(o)
    # a later force_next is the operator's "say whatever is next": the stronger correction goes first
    _, o = run(ev[:-len(ticks(15.5, 65))] + [(15.2, Control(flag="force_next"))])
    assert fired(o)[1][1:] == ("correction", "B", "c2")
