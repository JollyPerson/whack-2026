"""Contract tests for app/schemas.py: round-trips, leniency, and one sample per expected producer."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import latency
from app.bus import Bus, load_jsonl
from app.mocks import mock_ears
from app.schemas import (EVENT_TYPES, ArgMapSnapshot, BotLine, BotTalk, DebateResult, Claim, Control, DirectorDecision, DirectorStatus, Event,
                         Evidence, Interjection, Link, MapEdge, MapNode, ModuleStatus, Numeric, Pause,
                         ResearchProgress, ResearchRequest, FactSheet, Fact, Consistency, DebateSummary, SummaryPoint, DisputedPoint, Score, SessionStart, SourceCard, SpeakerInfo, TTSState, Utterance,
                         ValidationError, Verdict, parse_event)

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_762_500_000.0      # a plausible epoch second (Nov 2025)
EV = [Evidence(url="https://www.ons.gov.uk/x", title="ONS bulletin", quote="fell by 31%")]


def producer_samples() -> dict[str, Event]:
    """What each module is expected to put on the bus (keys: producer/event)."""
    claim = Claim(id="c7", utterance_id="u-A-0005", speaker="A", side="prop",
                  text="Bus use outside London has fallen by a third since 2010.", type="comparison",
                  checkable=True, links=[Link(claim_id="c3", rel="attacks")], importance=0.67,
                  topic="bus use outside London", t_end=T0 + 71.98, source="llm",
                  marks={"speech_end": T0 + 71.98, "stt_final": T0 + 72.78, "claim": T0 + 73.2})
    verdict = latency.finish(Verdict(claim_id="c7", label="Missing context", confidence=72, evidence=EV, via="web",
                                     explanation="Fell 31% but London excluded.", short_fact="it fell by about 31%",
                                     numeric=Numeric(claimed=33.3, actual=31.0, unit="%"),
                                     marks={"retrieved": T0 + 74.0}),
                             claim, now=T0 + 75.0)
    return {
        "mock_ears/Utterance": Utterance(id="u-A-0001", speaker="A", text="Thank you. Air pollution",
                                         t_start=T0, t_end=T0 + 2, final=False, ts=T0 + 2, src="mock_ears"),
        "ears/Pause": Pause(speaker="spk1", t=T0 + 5),
        "brain/Claim": claim,
        "evidence/Verdict": verdict,
        "evidence/SourceCard": SourceCard(claim_id="c7", url=EV[0].url, title="ONS", snippet="... 31% ..."),
        "evidence/ResearchProgress": ResearchProgress(stage="embedding", done=12, total=20, detail="ons.gov.uk"),
        "stage/ResearchRequest": ResearchRequest(motion="This house would ban cars", brief="Focus on UK cities."),
        "brain/Consistency": Consistency(id="k1", kind="self_contradiction", claim_id="c9", earlier_claim_id="c3",
                                         side="prop", speaker="A", now="cost two billion", then="cost almost nothing",
                                         explanation="Prop gave two incompatible costs.", confidence="85%"),
        "brain/DebateSummary": DebateSummary(motion="ban cars", version=12,
                                             agreed=[SummaryPoint(statement="Buses need investment", claim_ids=["c5", "c8"])],
                                             disputed=[DisputedPoint(issue="Effect on shops", prop="Footfall rises",
                                                                     opp="Shops lose trade", evidence_leans="prop")],
                                             self_contradictions={"prop": 1}, score={"prop": .6, "opp": .4}),
        "evidence/FactSheet": FactSheet(motion="This house would ban cars", facts=[
            Fact(statement="Bus use outside London fell 31% since 2010.", side="prop", quote="fell by 31%",
                 url=EV[0].url, title="ONS", number=31, unit="%")]),
        "director/Interjection": Interjection(id="i-0001", text="Point of fact: it fell by about 31%.",
                                              priority=0.83, claim_id="c7", kind="correction", target="A",
                                              evidence=EV[:1], marks={"pause": T0 + 79.4, "interject": T0 + 79.6}),
        "director/Interjection(prompt)": Interjection(id="i-0002", text="Opposition, you haven't answered on buses.",
                                                      priority=0.6, claim_id=None, kind="prompt", target="B"),
        "director/DirectorDecision": DirectorDecision(key="correction:c7", kind="correction", claim_id="c7",
                                                      action="held", reason="speaker_talking", score=0.83),
        "director/DirectorStatus": DirectorStatus(floor_open=True, tts_busy=False, cooldown_left_s=12.5, muted=False,
                                                  aggr=4, pending=[{"key": "prompt:c3", "kind": "prompt", "score": 0.6}],
                                                  pressure={"A": 1, "spk9": 2}),
        "brain/ArgMapSnapshot": ArgMapSnapshot(
            version=3, motion="ban cars",
            nodes=[MapNode(claim_id="c3", speaker="B", side="opp", text="70% arrive by car", status="unanswered",
                           verdict="supported", confidence=0.9, turn=2, importance=1.0),
                   MapNode(claim_id="c7", speaker="A", side="prop", status="minor", turn=3)],
            edges=[MapEdge(src="c7", dst="c3", relation="attacks")],
            score={"prop": Score(supported=2, score=0.61), "opp": Score(unanswered=1, score=0.39)},
            call="prop ahead on the evidence (3 checked claims)",
            turns=[{"idx": 3, "speaker": "A", "side": "prop", "claims": ["c7"], "closed": True}],
            nonresponsive=[{"turn": 3, "side": "prop", "ignored": ["c3"], "final": True}]),
        "dashboard/TTSState": TTSState(speaking=True, interjection_id="i-0001", src="dashboard:audio"),
        "dashboard/Control(aggr)": Control(flag="aggr", value=4),
        "dashboard/Control(veto)": Control(flag="veto", value="correction:c7"),
        "dashboard/Control(time)": Control(flag="time", value="spk2"),
        "dashboard/Control(mute)": Control(flag="mute", value=True),
        "dashboard/SessionStart": SessionStart(motion="This house would ban cars",
                                               speakers={"Gov": SpeakerInfo(name="Priya", side="prop"),
                                                         "spk2": {"name": "Tom", "side": "opp"}}),
        "main/ModuleStatus": ModuleStatus(module="brain", state="degraded", detail="fallback 3/41"),
        "bots/BotLine(rebut)": BotLine(persona="for", text="ONS puts it at 4.2%, not 6%. Try again.", kind="rebut",
                                       target="B", claim_id="c3", sourced=True, src="bots",
                                       evidence=[Evidence(url="https://www.ons.gov.uk/x", title="ONS", quote="4.2%")]),
        "bots/BotLine(banter)": BotLine(persona="against", text="Bold of you to cite a source.", kind="banter",
                                        reply_to="b-0001", hold_s=6.0, src="bots"),
        "bots/BotTalk": BotTalk(persona="for", line_id="b-0001", on=False, src="bots"),
        "judge/DebateResult": DebateResult(winner="prop", margin="clear", headline="Prop wins on the evidence.",
                                           scores={"prop": {"factuality": 8, "overall": 7.5},
                                                   "opp": {"factuality": 5, "overall": 5.5}},
                                           report="logs/summaries/20261108-run-summary.md",
                                           dances={"for": "MMD-Conqueror", "against": "MMD-Lost One_s Weeping"},
                                           music={"for": True, "against": False}, src="judge"),
    }


SAMPLES = producer_samples()


def test_samples_cover_every_known_event_type():
    missing = set(EVENT_TYPES) - {e.event for e in SAMPLES.values()}
    assert not missing, f"add a producer sample for {missing}"


@pytest.mark.parametrize("key", list(SAMPLES))
def test_round_trip(key):
    ev = SAMPLES[key]
    back = parse_event(ev.model_dump_json())
    assert type(back) is type(ev)
    assert back.model_dump() == ev.model_dump()
    assert parse_event(ev.model_dump(mode="json")).model_dump() == ev.model_dump()   # dict path too


def test_control_values_keep_their_json_type():
    for v in (4, "spk2", True, False, None, {"key": "x"}):
        assert parse_event(Control(flag="f", value=v).model_dump_json()).value == v
    assert type(parse_event(Control(flag="aggr", value=4).model_dump_json()).value) is int


def test_producer_sample_details():
    c, v = SAMPLES["brain/Claim"], SAMPLES["evidence/Verdict"]
    assert c.links[0].relation == "attacks" and 0 <= c.importance <= 1
    assert v.label == "missing_context" and v.confidence == pytest.approx(0.72)
    assert v.marks["speech_end"] == c.marks["speech_end"] and v.marks["retrieved"] == T0 + 74.0
    assert v.marks["verdict"] == T0 + 75.0 and v.latency_ms == round((75.0 - 71.98) * 1000)
    i = SAMPLES["director/Interjection"]
    assert i.id == "i-0001" and i.evidence[0].url.startswith("https://") and isinstance(i.priority, float)
    s = SAMPLES["dashboard/SessionStart"]
    assert s.speakers["spk2"].side == "opp" and s.speakers["Gov"].name == "Priya"


def test_link_aliases():
    for alias in ("relation", "rel", "type", "kind"):
        assert Link.model_validate({"claim_id": "c1", alias: "supports"}).relation == "supports"
    c = parse_event({"event": "Claim", "id": "c2", "speaker": "B", "text": "x", "links": [{"claim_id": "c1", "rel": "attacks"}]})
    assert c.links[0].relation == "attacks"
    assert json.loads(c.model_dump_json())["links"][0] == {"claim_id": "c1", "relation": "attacks"}
    with pytest.raises(ValidationError):
        Link(claim_id="c1", relation="agrees")


@pytest.mark.parametrize("raw,label", [
    ("Missing context", "missing_context"), ("TRUE", "supported"), ("false", "contradicted"),
    ("Misleading", "missing_context"), ("needs-context", "missing_context"), ("supported", "supported"),
    # fact-checker vocabularies (review: these all silently became unverifiable)
    ("Mostly true", "supported"), ("True", "supported"), ("Correct", "supported"), ("Accurate", "supported"),
    ("False", "contradicted"), ("Mostly false", "contradicted"), ("Pants on fire", "contradicted"),
    ("Incorrect", "contradicted"), ("Wrong", "contradicted"), ("REFUTES", "contradicted"),
    ("Half true", "missing_context"), ("Mixed", "missing_context"), ("Needs context", "missing_context"),
    ("Exaggerated", "missing_context"), ("Lacks context", "missing_context"), ("Partly false", "missing_context"),
    ("Unproven", "unverifiable"), ("Unverified", "unverifiable"), ("No evidence", "unverifiable"),
    ("NOT ENOUGH INFO", "unverifiable"),
    # case / punctuation insensitive
    ("Pants on Fire!", "contradicted"), ("HALF-TRUE", "missing_context"), (" mostly  true. ", "supported"),
    ("Needs_Context", "missing_context"), ("no-evidence", "unverifiable"),
])
def test_verdict_label_normalisation(raw, label):
    assert Verdict(claim_id="c1", label=raw, confidence=0.9, evidence=EV).label == label


def test_unknown_label_warns_once_and_guardrail_still_applies(caplog):
    for _ in range(3):
        assert Verdict(claim_id="c1", label="Four Pinocchios xyz", evidence=EV).label == "unverifiable"
    assert caplog.text.count("Four Pinocchios xyz") == 1
    v = parse_event({"event": "Verdict", "claim_id": "c1", "label": "Mostly false", "confidence": 0.9})
    assert (v.label, v.confidence) == ("unverifiable", 0.0)            # no evidence -> no verdict


def test_lenient_coercion_of_plausible_teammate_payloads():
    """Review (flexibility #6): payloads A/C plausibly send were rejected or misread."""
    u = parse_event({"event": "Utterance", "speaker": 0, "text": "Unemployment is", "is_final": False,
                     "t_start": None, "t_end": "1762532405900", "id": 7})
    assert (u.speaker, u.id, u.final, u.t_start, u.t_end) == ("0", "7", False, 0.0, pytest.approx(1762532405.9))
    assert "is_final" not in u.model_dump() and parse_event(u.model_dump_json()).final is False
    assert parse_event({"event": "Utterance", "speaker": "A", "text": "x", "isFinal": "false"}).final is False
    assert parse_event({"event": "Pause", "speaker": 1, "t": None}).speaker == "1"
    s = parse_event({"event": "SessionStart", "speakers": {0: {"name": "P", "side": "opp"}}, "motion": None})
    assert s.speakers["0"].side == "opp" and s.motion == ""
    v = parse_event({"event": "Verdict", "claim_id": 12, "label": "False", "confidence": "85", "latency_ms": 1234.6,
                     "explanation": None, "short_fact": None, "evidence": ["https://www.ons.gov.uk/x"]})
    assert (v.claim_id, v.label, v.confidence, v.latency_ms, v.explanation) == ("12", "contradicted", 0.85, 1235, "")
    assert v.evidence[0].url == "https://www.ons.gov.uk/x"
    v = parse_event({"event": "Verdict", "claim_id": "c1", "label": "supported", "confidence": "72%",
                     "evidence": {"link": "https://x.org", "title": "X"}, "latency_ms": "900"})
    assert (v.confidence, v.evidence[0].url, v.latency_ms) == (0.72, "https://x.org", 900)
    assert parse_event({"event": "Verdict", "claim_id": "c1", "confidence": None}).confidence == 0.0
    for raw, imp in ((2, 0.67), ("2", 0.67), ("3", 1.0), ("high", 1.0), ("0.4", 0.4), (-1, 0.0)):
        assert Claim(id="c1", speaker="A", text="x", importance=raw).importance == pytest.approx(imp, abs=0.01)
    assert Claim(id="c1", speaker="A", text="x", side=None).side is None    # nullable fields keep None
    assert Interjection(id="i", text="t", claim_id=None).claim_id is None
    with pytest.raises(ValidationError):
        parse_event({"event": "Utterance", "speaker": None, "text": "x"})   # null on a required field: still an error


def test_importance_one_to_three_scale_vs_unit_floats():
    """Int 1 (or "1") is the LOW end of the 1..3 scale, not 1.0; a float 1.0 is the 0..1 max."""
    imp = lambda raw: Claim(id="c1", speaker="A", text="x", importance=raw).importance
    for raw, want in ((1, 0.33), (2, 0.67), (3, 1.0), ("1", 0.33), (" 2 ", 0.67), ("3", 1.0),
                      (1.0, 1.0), (0.4, 0.4), (0, 0.0), (0.0, 0.0), ("0.5", 0.5), ("1.0", 1.0),
                      ("low", 0.33), ("Medium", 0.67), ("HIGH", 1.0),
                      (2.5, 0.83), (4, 1.0), (7.5, 1.0), ("9", 1.0), (-1, 0.0)):
        assert imp(raw) == pytest.approx(want, abs=0.01), raw
    for line, want in (('"importance":1', 0.33), ('"importance":1.0', 1.0), ('"importance":"1"', 0.33)):
        raw = '{"event":"Claim","id":"c1","speaker":"A","text":"x",%s}' % line   # JSON 1 is int, 1.0 is float
        assert parse_event(raw).importance == pytest.approx(want, abs=0.01), line


def test_control_values_on_bool_flags_are_bools():
    assert Control(flag="agent_speaking", value="false").value is False    # bool("false") would be True
    assert Control(flag="mute", value="ON").value is True and Control(flag="tts", value=0).value is False
    assert Control(flag="aggr", value="4").value == 4 and Control(flag="veto", value="correction:c7").value == "correction:c7"
    assert Control(flag="mute", value="maybe").value == "maybe"            # unknown words untouched


def test_confidence_percent_and_no_evidence_downgrade():
    assert Verdict(claim_id="c1", label="supported", confidence=85, evidence=EV).confidence == pytest.approx(0.85)
    assert Verdict(claim_id="c1", label="supported", confidence=150, evidence=EV).confidence == 1.0
    assert Verdict(claim_id="c1", label="supported", confidence=0.4, evidence=EV).confidence == 0.4
    v = parse_event({"event": "Verdict", "claim_id": "c1", "label": "contradicted", "confidence": 0.95})
    assert (v.label, v.confidence) == ("unverifiable", 0.0)   # every verdict must show its source


def test_millisecond_timestamps_become_seconds():
    u = parse_event({"event": "Utterance", "speaker": "A", "text": "x", "ts": 1_762_500_000_123,
                     "t_start": 1_762_499_998_000, "t_end": 1_762_499_999_500})
    assert u.ts == pytest.approx(1_762_500_000.123) and u.t_start == pytest.approx(1_762_499_998.0)
    assert u.t_end == pytest.approx(1_762_499_999.5)
    assert parse_event({"event": "Pause", "speaker": "A", "t": 1_762_500_000_000}).t == pytest.approx(1_762_500_000.0)
    assert parse_event({"event": "Pause", "speaker": "A", "t": 5.0}).t == 5.0   # relative fixture seconds untouched


def test_unknown_event_type_is_preserved():
    raw = {"event": "FaceReady", "seq": 0, "ts": T0, "src": "face", "voice": "en-GB", "nested": {"a": [1, 2]}}
    ev = parse_event(json.dumps(raw))
    assert type(ev) is Event and ev.model_extra == {"voice": "en-GB", "nested": {"a": [1, 2]}}
    assert json.loads(ev.model_dump_json()) == raw
    assert parse_event(ev.model_dump_json()).model_dump() == ev.model_dump()


def test_extra_fields_on_known_events_survive_json():
    c = Claim(id="c1", speaker="A", text="x", stance="pro", llm={"ms": 412})
    back = parse_event(c.model_dump_json())
    assert type(back) is Claim and back.stance == "pro" and back.llm == {"ms": 412}
    v = parse_event({"event": "Verdict", "claim_id": "c1", "retrieval_k": 5})
    assert v.model_extra == {"retrieval_k": 5}


def test_invalid_events_raise():
    with pytest.raises(ValidationError):
        parse_event({"event": "Claim", "id": "c1", "text": "no speaker"})
    with pytest.raises(ValidationError):
        parse_event('{"event": "TTSState"}')                # speaking is required
    for bad in ('{"speaker": "A"}', "[1, 2]", {"event": ""}):
        with pytest.raises(ValueError):
            parse_event(bad)
    with pytest.raises(ValueError):
        parse_event("not json")


def test_claim_types_and_opaque_speakers():
    for t in ("statistic", "comparison", "causal", "attribution", "fact", "prediction", "value", "other"):
        assert Claim(id="c1", speaker="Gov", text="x", type=t).type == t
    assert Utterance(speaker="spk1", text="x").speaker == "spk1"
    assert Claim(id="c1", speaker="A", text="x").importance == 0.5      # float 0..1 default


def test_mock_ears_output_validates():
    events = load_jsonl(ROOT / "fixtures" / "fake_debate.jsonl")
    sched = [e for _, e in mock_ears.schedule(events)]
    interims = [e for e in sched if isinstance(e, Utterance) and not e.final]
    finals = {e.id: e for e in sched if isinstance(e, Utterance) and e.final}
    assert interims and finals
    assert all(i.id in finals for i in interims)                        # interim revisions share the final's id
    for e in sched:
        assert parse_event(e.model_dump_json()).model_dump() == e.model_dump()


@pytest.mark.parametrize("path", sorted((ROOT / "fixtures").glob("**/*.jsonl")), ids=lambda p: p.name)
def test_every_fixture_line_parses(path):
    lines = [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("//")]
    for i, ln in enumerate(lines, 1):
        try:
            parse_event(ln)
        except ValueError as e:                                         # ValidationError is a ValueError
            pytest.fail(f"{path.name} line {i}: {e}")
    assert len(load_jsonl(path)) == len(lines)


def _log(tmp_path, rows):
    p = tmp_path / "run.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_full_log_as_fixture_only_reemits_speech(tmp_path):
    """Review: a run log copied in as the fixture replayed its old Claims/Verdicts/Interjections into drive."""
    ev = [{"url": "https://x.org"}]
    p = _log(tmp_path, [
        {"event": "SessionStart", "motion": "rehearsal", "ts": 1000.0, "src": "dashboard"},
        {"event": "Utterance", "id": "u0", "speaker": "A", "text": "old session", "ts": 1001.0, "src": "ears"},
        {"event": "SessionStart", "motion": "judging", "ts": 1100.0, "src": "dashboard"},
        {"event": "ModuleStatus", "module": "ears", "state": "down", "ts": 1100.5, "src": "main"},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "Bus use outside", "final": False,
         "t_start": 1100.6, "t_end": 1101.2, "ts": 1101.3, "src": "ears"},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "Bus use outside London fell by a third.",
         "t_start": 1100.6, "t_end": 1102.0, "ts": 1102.1, "src": "ears"},
        {"event": "Claim", "id": "c1", "speaker": "A", "text": "Bus use fell.", "ts": 1102.2, "src": "brain"},
        {"event": "Pause", "speaker": "A", "t": 1103.0, "ts": 1103.0, "src": "brain:pause_synth"},
        {"event": "Pause", "speaker": "A", "t": 1103.1, "ts": 1103.1, "src": "ears"},
        {"event": "Verdict", "claim_id": "c1", "label": "contradicted", "evidence": ev, "ts": 1104.0,
         "src": "evidence"},
        {"event": "Interjection", "id": "i-0001", "text": "Point of fact.", "ts": 1104.5, "src": "brain"},
        {"event": "ArgMapSnapshot", "version": 3, "ts": 1104.6, "src": "brain"},
        {"event": "Control", "flag": "mute", "value": True, "ts": 1105.0, "src": "dashboard"},
    ])
    sched = [e for _, e in mock_ears.schedule(load_jsonl(p))]
    assert [(e.event, getattr(e, "final", None)) for e in sched] == [
        ("SessionStart", None), ("Utterance", False), ("Utterance", True), ("Pause", None)]   # recorded interims kept
    assert sched[0].motion == "judging" and sched[-1].src == "ears"


async def test_mock_ears_cuts_idle_gaps(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_EARS_DELAY", "0")
    monkeypatch.setenv("FIXTURE_MAX_GAP", "0.05")
    monkeypatch.delenv("LOOP", raising=False)
    p = _log(tmp_path, [
        {"event": "SessionStart", "motion": "m", "ts": 0.0},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "two words", "t_start": 199.0, "t_end": 199.8,
         "ts": 200.0},                                                            # 200 s of setup silence
        {"event": "Pause", "speaker": "A", "t": 200.2, "ts": 200.2},
    ])
    b = Bus()
    s = b.subscribe("x")
    await asyncio.wait_for(mock_ears.run(b, p, speed=1), 2)
    got = [s.q.get_nowait() for _ in range(s.q.qsize())]
    assert [e.event for e in got] == ["SessionStart", "Utterance", "Pause"]
    assert got[2].ts - got[0].ts == pytest.approx(0.1, abs=0.04)


def test_bot_line_and_bot_talk_round_trip_with_defaults():
    from app.schemas import BotLine, BotTalk
    line = parse_event({"event": "BotLine", "persona": "for", "text": "Source for that 40%?"})
    assert isinstance(line, BotLine) and line.kind == "followup" and line.sourced is False and line.id.startswith("b-")
    assert line.hold_s == 8.0 and line.target is None and line.evidence == []
    assert parse_event(line.model_dump_json()).model_dump() == line.model_dump()
    talk = parse_event({"event": "BotTalk", "persona": "against", "line_id": line.id})
    assert isinstance(talk, BotTalk) and talk.on is False


def test_bots_control_flag_is_a_bool_flag():
    from app.schemas import Control
    assert Control(flag="bots", value="off").value is False and Control(flag="bots", value="1").value is True

