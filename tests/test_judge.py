"""app/judge: the post-debate scorecard, Claude's verdict and its fallbacks. The LLM is a FakeLLM with canned answers
(never the network)."""
from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path

import pytest

from app import config
from app.brain.llm import LLMError, LLMTimeout
from app.bus import load_jsonl
from app.judge import scorecard as sc, writer
from app.schemas import (ArgMapSnapshot, BotLine, Claim, Evidence, Interjection, MapNode, SessionStart, SpeakerInfo,
                         Utterance, Verdict)

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_762_600_000.0
SPEAKERS = {"A": SpeakerInfo(name="Alice Archer", side="prop"), "B": SpeakerInfo(name="Bob Baker", side="opp")}
ONS = [Evidence(url="https://www.ons.gov.uk/x", title="Labour market overview", quote="the rate was 4.2%")]
WIN, LOSE, DRAW = "MMD-Conqueror", "MMD-Lost One_s Weeping", "MMD-皆大欢喜-卜逻desu"


@pytest.fixture(autouse=True)
def _tunables(monkeypatch, tmp_path):
    for k, v in dict(JUDGE_DIR=str(tmp_path / "summaries"), JUDGE_TIMEOUT_S=5.0, JUDGE_DANCE_WIN=WIN,
                     JUDGE_DANCE_LOSE=LOSE, JUDGE_DANCE_DRAW=DRAW, JUDGE_DANCE_S=30.0, JUDGE_MUSIC="winner").items():
        monkeypatch.setattr(config, k, v)


def U(uid, spk, text, t0=0.0, t1=0.0, final=True):
    return Utterance(id=uid, speaker=spk, text=text, t_start=T0 + t0 if t0 else 0.0, t_end=T0 + t1 if t1 else 0.0,
                     final=final)


def C(cid, spk, side, typ="statistic", checkable=True, text=None):
    return Claim(id=cid, speaker=spk, side=side, type=typ, checkable=checkable, text=text or f"claim {cid}")


def V(cid, label, conf=0.9, evidence=ONS):
    return Verdict(claim_id=cid, label=label, confidence=conf, evidence=evidence, explanation=f"{cid} checked")


def synthetic() -> list:
    """A session with every kind of record, after a rehearsal that must not count."""
    return [
        SessionStart(motion="Rehearsal", speakers=SPEAKERS),
        U("u-A-0001", "A", "rehearsal words that never count", 1, 3),
        SessionStart(motion="This house would raise the minimum wage", speakers=SPEAKERS),
        U("u-A-0001", "A", "Unemployment is at four point two percent and falling fast", 1, 11),   # 10 s, 10 words
        U("u-B-0001", "B", "That figure is out of date", 12, 17),                                   # 5 s
        U("u-A-0002", "A", "Wages rose", 18, 19, final=False),                                     # an interim...
        U("u-A-0002", "A", "Wages rose by six percent last year", 20, 26),                         # ...its final, 6 s
        U("u-B-0002", "B", "Five words with no times"),                                           # 5 / 2.5 = 2 s
        C("c1", "A", "prop"), C("c2", "A", "prop", typ="value", checkable=False), C("c3", "B", "opp"),
        C("c4", "B", "opp", typ="comparison"), C("c5", "A", None),                                # side from speaker
        V("c1", "supported"), V("c3", "contradicted"), V("c4", "missing_context", conf=0.5),      # c4: low confidence
        V("c5", "supported"), V("c5", "contradicted"),                                            # the latest wins
        BotLine(id="b1", persona="for", text="6%? Last I checked nearer 4%. (unsourced)", kind="challenge",
                target="B", claim_id="c3", src="bots"),
        BotLine(id="b2", persona="against", text="Fair, that one checks out.", kind="concede", target="A",
                claim_id="c1", src="bots"),
        BotLine(id="b3", persona="for", text="Still waiting on the wage point.", kind="nudge", target="B",
                claim_id="c1", src="bots"),                                                       # c1 is OUR point
        BotLine(id="b4", persona="against", text="Bold of you.", kind="banter", reply_to="b3", src="bots"),
        BotLine(id="b5", persona="for", text="The ONS says 4.2%, not 6%.", kind="rebut", target="B", claim_id="c3",
                sourced=True, evidence=ONS, src="bots"),
        BotLine(id="b5", persona="for", text="The ONS says 4.2%, not 6%.", kind="rebut", target="B", claim_id="c3",
                sourced=True, evidence=ONS, src="bots"),                                          # a duplicate
        Interjection(id="i-1", text="Not quite: 4.2%.", kind="correction", claim_id="c3", target="B"),
        Interjection(id="i-2", text="Opposition, you haven't answered the wage point.", kind="prompt",
                     claim_id="c1", target="B"),                                                  # Prop's claim, at Opp
        Interjection(id="i-3", text="Thirty seconds left.", kind="time"),
        ArgMapSnapshot(version=1, nodes=[MapNode(claim_id="c1", speaker="A", side="prop", status="open")]),
        ArgMapSnapshot(version=2, nodes=[
            MapNode(claim_id="c1", speaker="A", side="prop", status="unanswered"),
            MapNode(claim_id="c3", speaker="B", side="opp", status="answered"),
            MapNode(claim_id="c4", speaker="B", side="opp", status="conceded"),
            MapNode(claim_id="c2", speaker="A", side="prop", status="minor")],
            nonresponsive=[{"turn": 1, "side": "opp", "ignored": ["c1"], "final": True},
                           {"turn": 3, "side": "prop", "ignored": ["c3"], "final": False}]),     # still open
    ]



def answer(winner="prop", margin="clear", prop=7.0, opp=5.0, **kw) -> dict:
    def side(ov):
        return {**{k: {"score": ov, "why": f"{k} because reasons"} for k in writer.CRITERIA}, "overall": ov}
    d = {"scores": {"prop": side(prop), "opp": side(opp)},
         "bias": {"prop": "Fair framing.", "opp": "Some loaded language.", "system": "Even-handed."},
         "factuality": "The Proposition's figures mostly held up.", "observations": ["A strong opening."],
         "reasoning": "The Proposition had 1 of 2 checked claims supported; the Opposition had 1 contradicted.",
         "winner": winner, "margin": margin, "headline": "The Proposition wins on the evidence."}
    d.update(kw)
    return d


class FakeLLM:
    name = "fake:judge"

    def __init__(self, out=None, delay=0.0, fail=None):
        self.out, self.delay, self.fail = answer() if out is None else out, delay, fail
        self.calls: list[tuple] = []

    async def complete_json(self, system, user, schema, timeout, max_tokens=400):
        self.calls.append((system, user, schema, timeout, max_tokens))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return copy.deepcopy(self.out)


# ----------------------------------------------------------------------------- the scorecard
def test_scorecard_on_a_synthetic_session():
    rec = sc.collect(synthetic())
    assert rec.motion == "This house would raise the minimum wage" and len(rec.utterances) == 4
    card = sc.scorecard(rec)
    p, o = card["sides"]["prop"], card["sides"]["opp"]
    assert p["speakers"] == ["Alice Archer"] and o["speakers"] == ["Bob Baker"]
    assert (p["speaking_s"], p["words"], p["utterances"]) == (16.0, 17, 2)          # no rehearsal, no interim
    assert (o["speaking_s"], o["words"]) == (7.0, 11)                               # 5 s + 5 words / 2.5
    assert (p["claims"], p["checkable"], o["claims"], o["checkable"]) == (3, 2, 2, 2)
    assert p["verdicts"] == {"supported": 1, "contradicted": 1, "missing_context": 0, "unverifiable": 0,
                             "with_source": 2, "low_conf": 0}
    assert o["verdicts"] == {"supported": 0, "contradicted": 1, "missing_context": 0, "unverifiable": 1,
                             "with_source": 2, "low_conf": 1}
    assert (p["ai_flagged"], p["ai_confirmed"], o["ai_flagged"], o["ai_confirmed"]) == (0, 1, 1, 0)
    assert (p["unanswered"], p["answered"], p["conceded"], p["ignored"], p["nonresponsive"]) == (1, 1, 1, 0, 0)
    assert (o["unanswered"], o["conceded_to"], o["ignored"], o["nonresponsive"]) == (0, 1, 1, 1)
    assert o["interjections"] == {"total": 2, "correction": 1, "prompt": 1} and p["interjections"] == {"total": 0}
    assert o["bot_lines"] == {"total": 3, "challenge": 1, "nudge": 1, "rebut": 1}
    assert p["bot_lines"] == {"total": 1, "concede": 1}
    assert p["criteria"] == {"factuality": 5.6, "evidence": 5.0, "responsiveness": 7.5, "balance": 10.0}
    assert o["criteria"] == {"factuality": 2.9, "evidence": 1.7, "responsiveness": 2.3, "balance": 8.6}
    assert p["overall"] == sc.overall(p["criteria"]) and o["overall"] == sc.overall(o["criteria"]) == 3.3
    assert card["formula"]["winner"] == "prop" and card["formula"]["margin"] == "decisive"
    s = card["system"]
    assert s["interventions"] == {"prop": 1, "opp": 5} and s["flagged"] == {"prop": 1, "opp": 2}
    assert s["interjections"]["none"] == 1 and s["bot_lines"]["banter"] == 1 and not s["even"]
    assert s["note"] == "more on the Opposition (5 to 1), in line with its flagged claims (2 to 1)"
    fc = sc.fact_checks(rec)
    assert [f["claim_id"] for f in fc["sourced"]] == ["c1", "c3", "c4", "c5"] and fc["unchecked"] == 1
    assert [(a["kind"], a["side"]) for a in fc["ai"]] == [("challenge", "opp"), ("concede", "prop")]


def test_scorecard_on_the_golden_run():
    rec = sc.collect(load_jsonl(ROOT / "fixtures" / "golden_run.jsonl"))
    card = sc.scorecard(rec)
    p, o = card["sides"]["prop"], card["sides"]["opp"]
    assert rec.motion == "This house would ban cars from city centres" and len(rec.utterances) == 12
    assert (p["claims"], p["checkable"], o["claims"], o["checkable"]) == (7, 6, 6, 2)
    assert (p["verdicts"]["supported"], p["verdicts"]["contradicted"], p["verdicts"]["missing_context"]) == (2, 3, 1)
    assert o["verdicts"]["low_conf"] == 1 and o["verdicts"]["unverifiable"] == 1      # c6 at 0.3 confidence
    assert (p["unanswered"], o["unanswered"], o["nonresponsive"]) == (2, 2, 1)
    assert o["interjections"] == {"total": 1, "prompt": 1}                         # "Opposition, you haven't..."
    assert (p["overall"], o["overall"]) == (5.4, 6.3)
    f = card["formula"]
    assert (f["winner"], f["margin"], f["difference"]) == ("opp", "clear", 0.9)
    assert 0 < p["speaking_s"] < 120 and p["share"] + o["share"] == pytest.approx(1.0, abs=0.01)


def test_formula_weights_draw_threshold_and_margin_bands():
    assert sum(sc.WEIGHTS.values()) == pytest.approx(1.0)
    assert sc.overall(dict.fromkeys(sc.CRITERIA, 10.0)) == 10.0
    assert sc.overall({"factuality": 8, "evidence": 6, "responsiveness": 4, "balance": 10}) == 6.8  # 2.8+1.5+1+1.5

    def f(prop, opp):
        return sc.formula({"prop": {"overall": prop}, "opp": {"overall": opp}})
    assert (f(6.0, 5.6)["winner"], f(6.0, 5.6)["margin"]) == ("draw", "")          # 0.4: narrow = draw
    assert (f(6.0, 5.5)["winner"], f(6.0, 5.5)["margin"]) == ("prop", "clear")     # 0.5 is not below the threshold
    assert (f(5.5, 7.4)["winner"], f(5.5, 7.4)["margin"]) == ("opp", "clear")
    assert (f(7.5, 5.5)["winner"], f(7.5, 5.5)["margin"]) == ("prop", "decisive")  # 2.0
    assert [sc.margin_of(g) for g in (0, 0.4, 0.5, 1.9, 2.0, -3)] == ["narrow", "narrow", "clear", "clear",
                                                                       "decisive", "decisive"]
    assert sc.margin_of(0.46) == "clear"                                         # gaps are taken to 0.1


def test_an_empty_record_is_neutral_and_a_draw():
    card = sc.scorecard(sc.collect([SessionStart(motion="m", speakers=SPEAKERS)]))
    assert card["sides"]["prop"]["criteria"] == card["sides"]["opp"]["criteria"]
    assert card["formula"]["winner"] == "draw"


def test_unknown_speakers_alternate_prop_then_opp():
    rec = sc.collect([SessionStart(motion="m"), U("u1", "X", "hello there"), U("u2", "Y", "hi"),
                      U("u3", "X", "again")])
    assert rec.sides == {"X": "prop", "Y": "opp"}


# ----------------------------------------------------------------------------- Claude's verdict
async def test_the_llm_judges_with_the_scorecard_fact_checks_and_transcript():
    rec = sc.collect(synthetic())
    card = sc.scorecard(rec)
    long = " ".join(["word"] * 30)
    llm = FakeLLM(answer(headline=long))
    j = await writer.decide(llm, rec, card)
    assert (j.via, j.winner, j.margin, j.model) == ("llm", "prop", "clear", "fake:judge")
    assert j.scores["prop"] == {**dict.fromkeys(writer.CRITERIA, 7.0), "overall": 7.0}
    assert j.why["opp"]["civility"] == "civility because reasons" and j.bias["system"] == "Even-handed."
    assert len(j.headline.split()) == writer.HEADLINE_WORDS
    system, user, schema, timeout, max_tokens = llm.calls[0]
    assert timeout == config.JUDGE_TIMEOUT_S and "same standard" in system
    assert "Motion: This house would raise the minimum wage" in user
    assert "Proposition (prop): Alice Archer" in user and '"formula":' in user
    assert "FACT CHECKS, sourced" in user and "FACT CHECKS, AI knowledge (unsourced" in user
    assert "c4 Opposition (Bob Baker)" in user and "counted as unverifiable" in user
    assert "Alice Archer (Proposition): Wages rose by six percent last year" in user and "rehearsal" not in user
    assert set(schema["properties"]["scores"]["properties"]["prop"]["required"]) == {*writer.CRITERIA, "overall"}


async def test_a_narrow_margin_is_a_draw():
    rec = sc.collect(synthetic())
    j = await writer.decide(FakeLLM(answer("prop", "narrow", prop=6.4, opp=6.1)), rec, sc.scorecard(rec))
    assert (j.via, j.winner, j.margin) == ("llm", "draw", "")
    assert "draw" in j.headline.lower() and "narrow" in j.note
    j = await writer.decide(FakeLLM(answer("draw", "narrow", prop=6.0, opp=6.0, headline="A draw.")), rec,
                            sc.scorecard(rec))
    assert (j.winner, j.margin, j.headline, j.note) == ("draw", "", "A draw.", "")


@pytest.mark.parametrize("out, why", [
    (answer("opp", "clear", prop=7.0, opp=5.0), "inconsistent"),                # its own overalls favour Prop
    (answer("prop", "narrow", prop=5.0, opp=6.0), "inconsistent"),              # even when narrow
    (answer("prop", "clear", prop=6.0, opp=6.0), "inconsistent"),               # a tie is no win
    (answer("draw", "narrow", prop=8.0, opp=5.0), "draw"),                      # a draw 3 points apart
    (answer(prop=11.0), "0 to 10"),
    (answer(reasoning="The Proposition was better overall."), "number"),       # cites no scorecard number
    (answer(winner="both"), "winner"),
    (answer(headline=""), "headline"),
    ({"winner": "prop"}, "scores"),
    (["not", "an", "object"], "object"),
])
async def test_an_invalid_or_inconsistent_answer_leaves_it_to_the_formula(out, why):
    rec = sc.collect(synthetic())
    card = sc.scorecard(rec)
    j = await writer.decide(FakeLLM(out), rec, card)
    assert (j.via, j.winner, j.margin) == ("formula", card["formula"]["winner"], card["formula"]["margin"])
    assert why in j.note and j.scores["prop"]["overall"] == card["sides"]["prop"]["overall"]
    assert j.criteria == sc.CRITERIA and "Proposition" in j.reasoning and "3.3" in j.reasoning


@pytest.mark.parametrize("llm, why", [
    (None, "no LLM"),
    (FakeLLM(delay=1.0), "within"),                       # slower than JUDGE_TIMEOUT_S
    (FakeLLM(fail=LLMTimeout("t")), "within"),
    (FakeLLM(fail=LLMError("HTTP 529")), "failed"),
])
async def test_no_llm_a_timeout_or_an_error_leaves_it_to_the_formula(monkeypatch, llm, why):
    monkeypatch.setattr(config, "JUDGE_TIMEOUT_S", 0.05)
    rec = sc.collect(synthetic())
    t = time.monotonic()
    j = await writer.decide(llm, rec, sc.scorecard(rec))
    assert time.monotonic() - t < 0.5
    assert (j.via, j.winner, j.margin) == ("formula", "prop", "decisive") and why in j.note
    assert j.headline == "Proposition wins decisively on the scorecard, 6.6 to 3.3."


def test_the_transcript_is_trimmed_in_the_middle_and_says_so():
    rec = sc.collect([SessionStart(motion="m", speakers=SPEAKERS),
                      *(U(f"u{i}", "AB"[i % 2], f"line {i} " + "x" * 80, i * 10 + 1, i * 10 + 5) for i in range(200))])
    lines, cut = writer.transcript(rec, budget=4000)
    assert cut > 0 and f"[... {cut} lines omitted for length ...]" in lines
    assert lines[0].startswith("[00:00] Alice Archer (Proposition): line 0 ")          # from the first speech
    assert "line 199 " in lines[-1] and sum(len(x) + 1 for x in lines) < 4200


def test_make_judge_llm_uses_the_judge_model_on_brains_client(monkeypatch):
    from app.brain.llm import AnthropicLLM

    class Client:
        pass
    monkeypatch.setattr(writer, "make_llm", lambda: AnthropicLLM(Client(), model="claude-haiku-5-5"))
    monkeypatch.setattr(config, "JUDGE_MODEL", "claude-opus-5-5")
    llm = writer.make_judge_llm()
    assert isinstance(llm, AnthropicLLM) and llm.model == "claude-opus-5-5" and llm.effort == writer.EFFORT
    monkeypatch.setattr(config, "JUDGE_MODEL", "opus")                            # a Claude Code alias
    assert writer.make_judge_llm().model == writer.DEFAULT_JUDGE_MODEL
    monkeypatch.setattr(writer, "make_llm", lambda: None)
    assert writer.make_judge_llm() is None
