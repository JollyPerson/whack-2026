"""app/bots: the For/Against side bots (wording, guards, pacing, the claim pipeline, nudges, U-turns and concessions,
banter, the final screen, wiring).

The engine runs on a fake clock that only moves when a test says so (the pacer's gaps, TTLs and hold_s); asyncio
waits (the verdict wait, LLM calls) stay real but tiny. The LLM is a FakeLLM with canned answers, never the network.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app import config
from app.bots import lines, writer as writer_mod
from app.bots.pacer import Pacer
from app.bots.run import Bots
from app.bots.writer import Writer
from app.brain.llm import AnthropicLLM, LLMTimeout
from app.bus import Bus
from app.schemas import (ArgMapSnapshot, BotLine, BotTalk, Claim, Consistency, Control, Evidence, MapNode, Numeric,
                         SessionStart, SpeakerInfo, Verdict)

T0 = 1_762_600_000.0
ONS = [Evidence(url="https://www.ons.gov.uk/x", title="Labour market overview", quote="the rate was 4.2%")]
SPEAKERS = {"A": SpeakerInfo(name="Alice Archer", side="prop"), "B": SpeakerInfo(name="Bob Baker", side="opp")}


@pytest.fixture(autouse=True)
def _tunables(monkeypatch):
    monkeypatch.setattr(config, "FLAGS", {"bots": True})
    for k, v in dict(BOTS_MIN_GAP_S=10.0, BOTS_LINE_TTL_S=30.0, BOTS_VERDICT_WAIT_S=0.05, BOTS_CHECK_MIN_CONF=0.80,
                     BOTS_MISLEADING_MIN_CONF=0.60, BOTS_BANTER=False, BOTS_BANTER_GAP_S=20.0).items():
        monkeypatch.setattr(config, k, v)


class FakeLLM:
    def __init__(self, question="Which baseline year are you using?", line="Bold of you to fact-check with vibes.",
                 check=None, fail=None, delay=0.0, name="fake"):
        self.question, self.line, self.check, self.fail, self.delay = question, line, check, fail, delay
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, schema, timeout, max_tokens=400):
        props = schema["properties"]
        kind = "check" if "assessment" in props else "banter" if "line" in props else "followup"
        self.calls.append((kind, user))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        if kind == "check":
            return self.check or check_answer("unsure", 0.5)
        return {"line": self.line} if kind == "banter" else {"question": self.question}


def check_answer(assessment, conf=0.95, line="", correction="", context=""):
    return {"assessment": assessment, "confidence": conf, "correction": correction, "context": context, "line": line}


def false_check(conf=0.95, challenge="6%? Last I checked it was nearer 4%.", correction="it was nearer 4%"):
    return check_answer("likely_false", conf, challenge, correction)


def misleading_check(conf=0.9, line="True, but that's against a modelled baseline.",
                     context="that's against a modelled baseline"):
    return check_answer("misleading", conf, line, context=context)


def surprising_check(conf=0.9, line="Annoyingly, that one checks out."):
    return check_answer("surprising_true", conf, line)


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(fast=None, check=None):
    """A Bots on a fake clock; published events land in `out`. Its own BotLines come back to it, like the bus."""
    out, clock, box = [], Clock(), {}

    def publish(ev):
        ev.src, ev.ts = ev.src or "bots", ev.ts or clock()
        out.append(ev)
        if ev.event == "BotLine":
            box["bots"].handle(ev)
        return ev

    bots = box["bots"] = Bots(publish, Writer(fast, check), clock)
    bots.handle(SessionStart(motion="This house would raise the minimum wage", speakers=SPEAKERS))
    return bots, out, clock


def C(cid, spk="B", side="opp", text="Unemployment is 6% in the UK.", typ="statistic", imp=1.0, checkable=True, **kw):
    return Claim(id=cid, speaker=spk, side=side, text=text, type=typ, importance=imp, checkable=checkable, **kw)


def V(cid, label="contradicted", **kw):
    kw.setdefault("evidence", ONS)
    kw.setdefault("short_fact", "unemployment was 4.2%")
    return Verdict(claim_id=cid, label=label, confidence=0.9, **kw)


def said(out, kind=None) -> list[BotLine]:
    return [e for e in out if e.event == "BotLine" and (kind is None or e.kind == kind)]


async def settle(bots: Bots, timeout: float = 2.0) -> None:
    """Until every per-claim task (and the checks they start) has finished."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while bots.tasks and loop.time() < end:
        await asyncio.wait(list(bots.tasks), timeout=end - loop.time())
    assert not bots.tasks, "bots tasks still running"


# ----------------------------------------------------------------------------- personas
async def test_each_bot_answers_only_the_opponent_and_a_claim_without_side_is_ignored():
    assert lines.answering("opp") == "for" and lines.answering("PROP") == "against"
    assert lines.answering(None) is None and lines.answering("") is None
    bots, out, clock = make()
    bots.handle(C("c1", "B", "opp", "Raising wages always destroys jobs.", typ="causal", checkable=False))
    bots.handle(C("c2", "A", "prop", "Low pay is a choice, not a law of nature.", typ="value", checkable=False))
    bots.handle(C("c3", "X", None, "Nobody owns this claim.", typ="value", checkable=False))
    await settle(bots)
    got = {(b.persona, b.target, b.claim_id) for b in said(out)}
    assert got == {("for", "B", "c1"), ("against", "A", "c2")}
    for b in said(out):
        assert lines.OWN_SIDE[b.persona] != bots.claims[b.claim_id].side and b.kind == "followup"


# ----------------------------------------------------------------------------- fact lines
async def test_a_sourced_verdict_becomes_a_rebut_that_quotes_its_source():
    check = FakeLLM(check=false_check())
    bots, out, clock = make(check=check)
    bots.handle(V("c1", numeric=Numeric(claimed=6, actual=4.2, unit="%")))    # raced ahead of its Claim
    bots.handle(C("c1"))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.claim_id, b.sourced) == ("rebut", "for", "B", "c1", True)
    assert b.text == "The ONS says 4.2%, not 6%. Try again." and b.evidence[0].url == ONS[0].url
    assert b.hold_s == lines.hold_s(b.text)
    clock.t += 30
    bots.tick()
    assert len(said(out)) == 1                                                # one fact line per claim


async def test_a_verdict_arriving_while_the_bot_waits_is_used():
    config.BOTS_VERDICT_WAIT_S = 5.0
    bots, out, clock = make()
    bots.handle(C("c1"))
    await asyncio.sleep(0.01)
    bots.handle(V("c1", label="missing_context", short_fact="that covers England only"))
    await settle(bots, 1.0)
    [b] = said(out)
    assert b.kind == "rebut" and b.text == "The ONS says that covers England only. Full picture, please."


async def test_a_capped_verdict_is_not_sourced_so_the_knowledge_check_answers():
    capped = V("c1", guard_notes=["capped"])
    assert not lines.sourced(capped) and lines.sourced(V("c1"))
    assert not lines.sourced(V("c1", evidence=[])) and not lines.sourced(V("c1", label="supported"))
    bots, out, clock = make(check=FakeLLM(check=false_check()))
    bots.handle(capped)
    bots.handle(C("c1"))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.sourced, b.evidence) == ("challenge", False, [])
    assert b.text == "6%? Last I checked it was nearer 4%. (unsourced)"


@pytest.mark.parametrize("check,kind", [
    (false_check(0.60), "followup"),                       # a contested claim: the misleading bar is not its bar
    (false_check(0.79), "followup"),                       # below BOTS_CHECK_MIN_CONF: no challenge
    (false_check(0.80), "challenge"),
    (false_check("90%"), "challenge"),                     # percentages are read as 0..1
    (check_answer("likely_true", 0.99), "followup"),
    (false_check(0.95, challenge="That's a lie, it was 4%."), "challenge"),   # its line fails: built from correction
])
async def test_the_ai_knowledge_challenge_fires_only_when_confident_and_is_tagged(check, kind):
    bots, out, clock = make(fast=FakeLLM(), check=FakeLLM(check=check))
    bots.handle(C("c1"))
    await settle(bots)
    [b] = said(out)
    assert b.kind == kind and not b.sourced
    if kind == "challenge":
        assert b.text.endswith(" (unsourced)") and b.text.count("(unsourced)") == 1
        assert "lie" not in b.text.lower()
    else:
        assert "(unsourced)" not in b.text


async def test_an_unverifiable_verdict_ends_the_wait_at_once():
    config.BOTS_VERDICT_WAIT_S = 5.0
    bots, out, clock = make(check=FakeLLM(check=false_check()))
    bots.handle(V("c1", label="unverifiable", evidence=[]))
    bots.handle(C("c1"))
    await settle(bots, 1.0)                                # well inside the 5 s wait
    assert [b.kind for b in said(out)] == ["challenge"]


async def test_a_supported_claim_gets_no_fact_line_but_may_get_a_followup():
    check = FakeLLM(check=false_check(0.99))
    bots, out, clock = make(check=check)
    bots.handle(V("c1", label="supported"))
    bots.handle(C("c1"))
    await settle(bots)
    [b] = said(out)
    assert b.kind == "followup" and b.text in lines.GRANTED


async def test_low_importance_claims_get_only_a_sourced_rebut():
    bots, out, clock = make(fast=FakeLLM(), check=FakeLLM(check=false_check()))
    bots.handle(C("c1", imp=0.33))
    bots.handle(C("c2", imp=0.33, text="Wages rose 2% last year.", checkable=False, typ="value"))
    await settle(bots)
    assert said(out) == [] and bots.writer.checker.calls == [] and bots.writer.fast.calls == []
    bots.handle(V("c3"))
    bots.handle(C("c3", imp=0.33))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["rebut"]


ULEZ = "London's ULEZ cut nitrogen dioxide in central London by 46 per cent."
STROKE = "Lightning often strikes the same place twice."


@pytest.mark.parametrize("conf,kind", [(0.59, "followup"), (0.60, "challenge"), (0.95, "challenge")])   # own bar
@pytest.mark.parametrize("spk,side,persona", [("B", "opp", "for"), ("A", "prop", "against")])
async def test_a_misleading_claim_gets_a_true_but_challenge_from_the_opposing_bot(conf, kind, spk, side, persona):
    bots, out, clock = make(fast=FakeLLM(), check=FakeLLM(check=misleading_check(conf)))
    bots.handle(C("c1", spk, side, ULEZ))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.sourced, b.evidence) == (kind, persona, spk, False, [])
    if kind == "challenge":
        assert b.text == "True, but that's against a modelled baseline. (unsourced)"
    else:
        assert "(unsourced)" not in b.text


@pytest.mark.parametrize("check,kind", [
    (misleading_check(0.85), "followup"), (misleading_check(0.90), "challenge"),
    (false_check(0.85), "challenge"), (surprising_check(0.85), "concede"),
])
async def test_the_misleading_bar_and_the_check_bar_are_tuned_apart(check, kind):
    config.BOTS_MISLEADING_MIN_CONF = 0.90                                    # above BOTS_CHECK_MIN_CONF (0.80)
    bots, out, clock = make(fast=FakeLLM(), check=FakeLLM(check=check))
    bots.handle(C("c1", text=ULEZ))
    await settle(bots)
    assert [b.kind for b in said(out)] == [kind]


@pytest.mark.parametrize("line,text", [
    ("True, but that's against a modelled baseline. (Unsourced)",               # its own tag, once
     "True, but that's against a modelled baseline. (unsourced)"),
    ("46%? True, but only 20% was measured.",                                    # a figure beyond claim + context
     "46 per cent? True, but that's against a modelled baseline. (unsourced)"),
    ("Sure, but that's a misleading baseline.",                                 # a judgement word
     "46 per cent? True, but that's against a modelled baseline. (unsourced)"),
    ("", "46 per cent? True, but that's against a modelled baseline. (unsourced)"),
])
async def test_a_misleading_challenge_carries_only_the_claims_and_its_context_figures(line, text):
    bots, out, clock = make(check=FakeLLM(check=misleading_check(line=line)))
    bots.handle(C("c1", text=ULEZ))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.text) == ("challenge", text)
    assert lines.misleading(C("c", text=ULEZ), "True, but a modelled 46 per cent.", "") == \
        "True, but a modelled 46 per cent. (unsourced)"
    assert lines.misleading(C("c", text=ULEZ), "", "True, but it's against a modelled scenario") == \
        "46 per cent? True, but it's against a modelled scenario. (unsourced)"
    assert lines.misleading(C("c", text=ULEZ), "", "") is None


@pytest.mark.parametrize("check,kind", [
    (surprising_check(0.79), "followup"),                                      # below the bar
    (surprising_check(0.80), "concede"),
    (check_answer("likely_true", 0.99, "Annoyingly, that one checks out."), "followup"),   # a plain truth: no concede
    (check_answer("unsure", 0.99, "Annoyingly, that one checks out."), "followup"),
])
async def test_a_surprising_truth_gets_a_grudging_concede_only_when_confident(check, kind):
    bots, out, clock = make(fast=FakeLLM(), check=FakeLLM(check=check))
    bots.handle(C("c1", "A", "prop", STROKE, typ="fact"))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.sourced) == (kind, "against", "A", False)
    if kind == "concede":
        assert b.text == "Annoyingly, that one checks out. (unsourced)"
    else:
        assert "(unsourced)" not in b.text and "checks out" not in b.text


def test_a_concede_carries_only_the_claims_figures():
    claim = C("c1", text="Over 70 percent of the Earth's surface is water.")
    assert lines.concede(claim, "70 percent? Annoyingly, yes.") == "70 percent? Annoyingly, yes. (unsourced)"
    assert lines.concede(claim, "Yes, about 71% actually.") == "Annoyingly, that one checks out. (unsourced)"
    assert lines.concede(claim, "Fair. Annoyingly, that's right. (Unsourced).") == \
        "Fair. Annoyingly, that's right. (unsourced)"
    assert lines.concede(claim, "{x} checks out") == "Annoyingly, that one checks out. (unsourced)"   # braces: odd
    assert lines.concede(claim, "Bob, that checks out.", names=["Bob"]) == \
        "Annoyingly, that one checks out. (unsourced)"
    assert lines.concede(claim, used=["Annoyingly, that one checks out. (unsourced)"]) == \
        "Fine, that one is actually true. (unsourced)"
    assert lines.guard("Annoyingly, it checks out.", "concede") is None                       # untagged
    assert lines.guard("Fine, 71% checks out. (unsourced)", "concede", claim.text) is None    # Claude's figure
    assert lines.guard("Fine, 70 percent checks out. (unsourced)", "concede", claim.text)


@pytest.mark.parametrize("check", [false_check(), misleading_check(), surprising_check()])
async def test_a_sourced_verdict_wins_over_every_knowledge_line(check):
    config.BOTS_VERDICT_WAIT_S = 5.0
    checker = FakeLLM(check=check, delay=0.05)
    bots, out, clock = make(check=checker)
    bots.handle(C("c1", text=ULEZ))
    await asyncio.sleep(0.01)
    bots.handle(V("c1", label="missing_context", short_fact="the 46% is against a modelled scenario"))
    await settle(bots, 1.0)
    [b] = said(out)
    assert (b.kind, b.sourced) == ("rebut", True)


CAPPED = ["capped"]


@pytest.mark.parametrize("check,verdict,kind", [
    (misleading_check(0.99), dict(label="supported"), "followup"),      # supported and sourced: never "true, but"
    (false_check(0.99), dict(label="supported"), "followup"),
    (surprising_check(0.99), dict(label="supported"), "followup"),      # sourced and supported: no unsourced concede
    (surprising_check(0.99), dict(label="contradicted", guard_notes=CAPPED), "followup"),   # capped, still disagrees
    (misleading_check(0.99), dict(label="contradicted", guard_notes=CAPPED), "followup"),   # "true, but" grants it
    (false_check(0.99), dict(label="contradicted", guard_notes=CAPPED), "challenge"),       # capped, and it agrees
    (misleading_check(0.99), dict(label="missing_context", guard_notes=CAPPED), "challenge"),
    (surprising_check(0.99), dict(label="unverifiable", evidence=[]), "concede"),
])
async def test_a_knowledge_line_never_contradicts_the_claims_verdict(check, verdict, kind):
    bots, out, clock = make(check=FakeLLM(check=check))
    bots.handle(V("c1", **verdict))
    bots.handle(C("c1", text=ULEZ))
    await settle(bots)
    [b] = said(out)
    assert b.kind == kind and not b.sourced
    if lines.supported(bots.verdicts["c1"]):
        assert b.text in lines.GRANTED
    if kind == "followup":
        assert "(unsourced)" not in b.text


@pytest.mark.parametrize("check,label,kind", [
    (false_check(0.99), "supported", "challenge"),
    (misleading_check(0.99), "supported", "challenge"),
    (misleading_check(0.99), "contradicted", "challenge"),
    (surprising_check(0.99), "contradicted", "concede"),
    (surprising_check(0.99), "missing_context", "concede"),
])
async def test_knowledge_drops_a_line_against_every_verdict_label_it_would_contradict(check, label, kind):
    """Every DISAGREES entry, even "supported", which the bus never brings this far (a supported Verdict always has
    evidence, so it cancels the check first)."""
    bots, out, clock = make()
    claim, res = C("c1", text=ULEZ), writer_mod.Check(**check)
    assert bots.knowledge(claim, res, Verdict.model_construct(claim_id="c1", label=label, evidence=[])) == \
        (kind, None, f"the Verdict says {label}")
    got, text, why = bots.knowledge(claim, res, None)
    assert (got, why) == (kind, "") and text.endswith(" (unsourced)")


@pytest.mark.parametrize("check,verdict,kind,log", [
    (false_check(), dict(), "rebut", "likely_false 0.95 (check bar 0.80) -> not fired: sourced verdict"),
    (surprising_check(), dict(guard_notes=CAPPED), "followup",
     "surprising_true 0.90 (check bar 0.80) -> not fired: the Verdict says contradicted"),
    (misleading_check(), dict(label="supported"), "followup",
     "misleading 0.90 (misleading bar 0.60) -> not fired: supported verdict"),
    (misleading_check(), dict(label="missing_context", guard_notes=CAPPED), "challenge",
     "misleading 0.90 (misleading bar 0.60) -> fired challenge"),
])
async def test_a_verdict_that_comes_in_while_the_check_outlasts_the_wait_still_wins(caplog, check, verdict, kind, log):
    caplog.set_level(logging.INFO, logger="bots")
    bots, out, clock = make(check=FakeLLM(check=check, delay=0.3))     # the check (0.3 s) outlasts the wait (0.05 s)
    bots.handle(C("c1", text=ULEZ))
    await asyncio.sleep(0.15)
    assert "c1" not in bots.waiting and not said(out)
    bots.handle(V("c1", **verdict))
    await settle(bots, 1.0)
    [b] = said(out)
    assert b.kind == kind and b.sourced == (kind == "rebut")
    assert [r.getMessage() for r in caplog.records if r.name == "bots"] == [f"bots: check c1: {log}"]


def test_one_fact_line_per_claim_across_rebut_challenge_and_concede():
    kinds = ("rebut", "challenge", "concede")
    for first in kinds:
        for second in kinds:
            p = Pacer(min_gap=1, ttl=30)
            assert p.push(L(text=f"First {first}. (unsourced)", kind=first, claim_id="c1", hold=1), 0)
            p.tick(0)
            assert not p.push(L(text=f"Then {second}. (unsourced)", kind=second, claim_id="c1"), 1)
            assert p.push(L(text=f"Then {second} on c2. (unsourced)", kind=second, claim_id="c2"), 1)
            assert p.push(L(text="Source for that?", claim_id="c1"), 1)              # a follow-up is not a fact line
    p = Pacer(min_gap=10, ttl=30)                                                    # both queued: the best one wins
    p.push(L(text="Concede. (unsourced)", kind="concede", claim_id="c1"), 0)
    p.push(L(text="Rebut.", kind="rebut", claim_id="c1"), 0)
    assert posted(p.tick(0)) == ["Rebut."] and posted(p.tick(20)) == []


def test_priority_order():
    assert sorted(lines.PRIORITY, key=lines.PRIORITY.get, reverse=True) == [
        "rebut", "uturn", "challenge", "concede", "nudge", "conceded", "followup", "banter"]
    assert lines.FACT_KINDS == ("rebut", "challenge", "concede") and lines.TAGGED == ("challenge", "concede")
    p = Pacer(min_gap=10, ttl=600)
    p.push(L(text="First?"), 0)
    p.tick(0)
    for kind in ("followup", "conceded", "concede", "nudge", "uturn", "challenge", "rebut"):
        tag = " (unsourced)" if kind in lines.TAGGED else ""
        assert p.push(L(text=f"A {kind} line.{tag}", kind=kind, claim_id=f"c-{kind}"), 1)
    assert [p.queued("for")[0].kind, len(p.queued("for"))] == ["followup", 7]
    assert [b.kind for t in range(10, 80, 10) for b in p.tick(t) if isinstance(b, BotLine)] == [
        "rebut", "uturn", "challenge", "concede", "nudge", "conceded", "followup"]


@pytest.mark.parametrize("check,verdict,want", [
    (false_check(0.95), None, "bots: check c1: likely_false 0.95 (check bar 0.80) -> fired challenge"),
    (false_check(0.60), None, "bots: check c1: likely_false 0.60 (check bar 0.80) -> not fired: below the bar"),
    (misleading_check(0.59), None, "bots: check c1: misleading 0.59 (misleading bar 0.60) -> not fired: below the bar"),
    (misleading_check(0.60), None, "bots: check c1: misleading 0.60 (misleading bar 0.60) -> fired challenge"),
    (surprising_check(0.9), None, "bots: check c1: surprising_true 0.90 (check bar 0.80) -> fired concede"),
    (surprising_check(0.79), None, "bots: check c1: surprising_true 0.79 (check bar 0.80) -> not fired: below the bar"),
    (check_answer("likely_true", 0.99), None, "bots: check c1: likely_true 0.99 -> not fired: likely_true"),
    (false_check(0.9, challenge="", correction="that is wrong"), None,
     "bots: check c1: likely_false 0.90 (check bar 0.80) -> not fired: dropped by the guards"),
    (surprising_check(0.9), "contradicted", "bots: check c1: surprising_true 0.90 (check bar 0.80) -> not fired: the "
                                            "Verdict says contradicted"),
    (false_check(0.9), "supported", "bots: check c1: no result -> not fired: supported verdict"),
    (None, None, "bots: check c1: no result -> not fired: no answer (timeout or error)"),
])
async def test_each_knowledge_check_logs_one_info_line(caplog, check, verdict, want):
    caplog.set_level(logging.INFO, logger="bots")
    checker = FakeLLM(check=check, fail=None if check else RuntimeError("boom"))
    bots, out, clock = make(check=checker)
    if verdict:
        bots.handle(V("c1", label=verdict, guard_notes=["capped"]))
    bots.handle(C("c1"))
    await settle(bots)
    got = [r.getMessage() for r in caplog.records if r.name == "bots" and "check" in r.getMessage()]
    assert got == [want] and all(r.levelno == logging.INFO for r in caplog.records if r.name == "bots")


async def test_the_check_log_line_shows_a_finished_check_that_a_sourced_verdict_beat(caplog):
    caplog.set_level(logging.INFO, logger="bots")
    config.BOTS_VERDICT_WAIT_S = 5.0
    bots, out, clock = make(check=FakeLLM(check=misleading_check(0.8)))
    bots.handle(C("c1"))
    await asyncio.sleep(0.02)                                                   # the check is back; no verdict yet
    bots.handle(V("c1"))
    await settle(bots, 1.0)
    assert [b.kind for b in said(out)] == ["rebut"]
    assert [r.getMessage() for r in caplog.records if r.name == "bots"] == [
        "bots: check c1: misleading 0.80 (misleading bar 0.60) -> not fired: sourced verdict"]


# ----------------------------------------------------------------------------- follow-ups
@pytest.mark.parametrize("fast,text", [
    (None, "Source for that 6%?"),                                              # no LLM: templates
    (FakeLLM(fail=LLMTimeout("slow")), "Source for that 6%?"),                  # LLM times out
    (FakeLLM(fail=RuntimeError("boom")), "Source for that 6%?"),                # any error
    (FakeLLM(question="Is that a lie, or just a guess?"), "Source for that 6%?"),   # judgement word
    (FakeLLM(question="Wasn't it 40% last year?"), "Source for that 6%?"),      # a figure the claim lacks
    (FakeLLM(question="Did his figure include part-time work?"), "Source for that 6%?"),   # about a person
    (FakeLLM(question="Did you just make that up?"), "Source for that 6%?"),   # their honesty
    (FakeLLM(question="Do you even understand what a baseline is?"), "Source for that 6%?"),   # their wits
    (FakeLLM(question="Isn't it nearer four?"), "Source for that 6%?"),        # Claude's own figure, untagged
    (FakeLLM(question="Which month is that 6% from?"), "Which month is that 6% from?"),
])
async def test_followup_falls_back_to_a_template_when_the_llm_fails_or_breaks_a_rule(fast, text):
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1"))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.text, b.target) == ("followup", text, "B")


def test_followup_templates_by_type_skip_used_lines():
    stat = C("c1")
    assert lines.followup(stat) == "Source for that 6%?"
    assert lines.followup(stat, ["Source for that 6%?"]) == "Where does that 6% come from?"
    assert lines.followup(C("c2", text="Wages rose in 2019.")) == "Source for that number?"    # a year isn't a figure
    for typ in ("comparison", "causal", "attribution", "fact", "prediction", "value", "other", "unknown"):
        line = lines.followup(C("c", typ=typ, text="Something was said."))
        assert line and lines.guard(line) == line
    assert lines.followup(stat, [t.format(figure="6%") for t in lines.FOLLOWUP["statistic"]]) is None


# ----------------------------------------------------------------------------- guards
@pytest.mark.parametrize("text,ok", [
    ("Bold of you to fact-check with vibes.", True),
    ("Source for that 40%? I'll wait.", True),                                  # the figure is in X's line
    ("Fine, but 75% of nothing is still nothing.", False),                      # a new figure
    ("Seventy percent of that was vibes.", False),                              # a new figure in words
    ("Check bbc.co.uk before you heckle.", False),                              # URL
    ("The ONS would like a word.", False),                                      # a source
    ("According to my notes, you're bluffing.", False),
    ("Calm down, you clown.", False),                                           # insult
    ("Nice try, but that is simply false.", False),                             # judgement word
    ("Bob Baker would never say that.", False),                                 # a debater's name
    ("Her confidence is doing a lot of work.", False),                          # about a person
    ("Sure, and I'm the referee's favourite toaster, apparently, if we are all being very honest here.", False),
    ("Bold claim 😂", False),
    ("Steady on, it was a question, not a closing speech.", True),
    ("Your debater is lazy and weak.", False),                                  # the debaters, judged
    ("Your debater is clearly panicking now.", False),
    ("Their speaker is flailing badly tonight.", False),
    ("Your speaker's arrogance is showing.", False),
    ("Spoken like a true bully.", False),
    ("Tell that to the Opposition.", False),                                    # any mention of the debaters
    ("Two questions in and still no answer.", False),                           # small numbers in words
    ("Ten out of ten for confidence.", False),
    ("Half the country would disagree with you.", False),
    ("Check bit.ly/abc before heckling.", False),                               # short links, other TLDs
    ("Ask claude.ai about that.", False),
])
def test_banter_guards(text, ok):
    got = lines.guard(text, "banter", "Source for that 40%?", strict=True, names=["Bob", "Baker"])
    assert (got == text) is ok


def test_strict_followup_guards_keep_plain_questions():
    for text in ("Which one: headline or core inflation?", "How many people does that 6% cover?",
                 "Is that two years of data or one?", "Compared with what, e.g. the 2010 level?"):
        assert lines.guard(text, "followup", "Over two years, 6% since 2010", strict=True, names=["Bob"]) == text


def test_challenge_tags_once_and_never_names_a_debater():
    claim = C("c1")
    for raw in ("6%? Last I checked it was nearer 4%. (Unsourced)", "6%? Last I checked it was nearer 4%. (unsourced).",
                "Unsourced: 6%? Last I checked it was nearer 4%."):
        assert lines.challenge(claim, raw) == "6%? Last I checked it was nearer 4%. (unsourced)"
    got = lines.challenge(claim, "Bob, last I checked it was nearer 4%.", "it was nearer 4%", names=["Bob", "Baker"])
    assert got == "6%? Last I checked, it was nearer 4%. (unsourced)"
    assert lines.challenge(claim, "Bob, last I checked it was nearer 4%.", names=["Bob"]) is None


def test_line_guards_lengths_tags_and_hold():
    assert lines.guard(" ".join(["word"] * 15)) and not lines.guard(" ".join(["word"] * 16))
    assert lines.guard(" ".join(["word"] * 18), "rebut") and not lines.guard(" ".join(["word"] * 19), "rebut")
    assert lines.guard("Nearer 4%, last I checked. (unsourced)", "challenge")                 # Claude's figure, tagged
    assert not lines.guard("Nearer 4%, last I checked.", "challenge")                         # untagged
    assert not lines.guard("Nearer 4%, last I checked.", "followup", "6%")                    # a new figure untagged
    assert lines.guard('"Quoted line?"') == "Quoted line?"
    assert lines.hold_s("a b c") == 6.0 and lines.hold_s(" ".join(["w"] * 15)) == 9.0
    assert lines.hold_s(" ".join(["w"] * 40)) == 12.0
    assert lines.nudge("graduate debt") == "Still waiting on the graduate debt point."
    assert lines.nudge("") == "Still waiting on our last point."
    assert lines.nudge("absurd claims") == "Still waiting on our last point."                # a judgement topic


# ----------------------------------------------------------------------------- pacer
def L(persona="for", text="Line?", kind="followup", claim_id=None, hold=6.0) -> BotLine:
    return BotLine(persona=persona, text=text, kind=kind, claim_id=claim_id, hold_s=hold)


def posted(evs) -> list[str]:
    return [e.text for e in evs if isinstance(e, BotLine)]


def test_pacer_posts_at_once_then_at_most_one_bubble_per_gap_per_bot():
    p = Pacer(min_gap=10, ttl=30)
    p.push(L(text="One?"), 0)
    p.push(L(text="Two?"), 0)
    p.push(L("against", "Other side?"), 0)
    assert posted(p.tick(0)) == ["One?", "Other side?"]
    assert posted(p.tick(9.9)) == []
    assert posted(p.tick(10)) == ["Two?"]
    p.push(L(text="Long one?", hold=12), 20)
    assert posted(p.tick(20)) == ["Long one?"]
    p.push(L(text="Next?"), 21)
    assert posted(p.tick(30)) == [] and posted(p.tick(32)) == ["Next?"]          # never replaces a bubble still up


def test_pacer_ttl_banter_sooner_and_priority():
    p = Pacer(min_gap=10, ttl=5)
    p.push(L(text="First?"), 0)
    p.tick(0)
    p.push(L(text="Stale?"), 1)
    assert posted(p.tick(10)) == []                                             # expired after 5 s
    p = Pacer(min_gap=10, ttl=60, banter_ttl=8)
    p.push(L(text="First?"), 0)
    p.tick(0)
    p.push(L(text="Quip.", kind="banter"), 1)
    p.push(L(text="Old follow-up?"), 1)
    assert posted(p.tick(10)) == ["Old follow-up?"] and p.queued("for") == []   # the banter expired after 8 s
    p.push(L(text="Follow?"), 20)
    p.push(L(text="Nudge.", kind="nudge"), 20)
    p.push(L(text="Banter.", kind="banter"), 20)
    p.push(L(text="Rebut.", kind="rebut", claim_id="c1"), 20)
    p.push(L(text="Concede. (unsourced)", kind="concede", claim_id="c3"), 20)
    p.push(L(text="Challenge. (unsourced)", kind="challenge", claim_id="c2"), 20)
    p.push(L(text="Newer follow?"), 21)
    assert [posted(p.tick(t)) for t in (21, 31, 41, 51, 61, 71)] == [
        ["Rebut."], ["Challenge. (unsourced)"], ["Concede. (unsourced)"], ["Nudge."], ["Newer follow?"],
        ["Follow?"]]                                                            # then the newest


def test_pacer_one_fact_line_per_claim_and_no_duplicate_text():
    p = Pacer(min_gap=1, ttl=30)
    p.push(L(text="The ONS says 4%. Try again.", kind="rebut", claim_id="c1", hold=1), 0)
    p.tick(0)
    assert not p.push(L(text="Nearer 4%. (unsourced)", kind="challenge", claim_id="c1"), 1)
    assert p.push(L(text="Source for that 6%?", claim_id="c1", hold=1), 1)     # a follow-up on it is fine
    assert not p.push(L("against", text="the ons says 4%. try again"), 1)       # same text, any bot
    p.push(L(text="Twice?", hold=1), 2)
    p.push(L(text="Twice?", hold=1), 2)
    assert posted(p.tick(2)) == ["Twice?"] and posted(p.tick(3)) == ["Source for that 6%?"]
    assert posted(p.tick(10)) == []


def test_pacer_closes_the_mouth_after_hold_s_and_clear_returns_open_mouths():
    p = Pacer(min_gap=10, ttl=30)
    line = L(hold=7.5)
    p.push(line, 0)
    assert p.tick(0) == [line] and p.tick(7.4) == []
    [off] = p.tick(7.5)
    assert isinstance(off, BotTalk) and (off.persona, off.line_id, off.on) == ("for", line.id, False)
    p.push(L(text="Again?"), 10)
    p.tick(10)
    p.push(L(text="Queued?"), 11)
    [off] = p.clear()
    assert off.line_id != line.id and p.queued("for") == [] and p.tick(30) == []
    p.paused = True
    assert not p.push(L(text="Muted?"), 31)


async def test_engine_publishes_bot_talk_off_after_hold_s():
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    [b] = said(out)
    clock.t = T0 + b.hold_s - 0.1
    bots.tick()
    assert not [e for e in out if e.event == "BotTalk"]
    clock.t = T0 + b.hold_s
    bots.tick()
    [talk] = [e for e in out if e.event == "BotTalk"]
    assert (talk.persona, talk.line_id, talk.on, talk.src) == ("for", b.id, False, "bots")


# ----------------------------------------------------------------------------- nudges
def node(cid, side, status, spk=None, imp=1.0, topic="", text="a point"):
    return MapNode(claim_id=cid, speaker=spk or ("A" if side == "prop" else "B"), side=side, text=text,
                   status=status, importance=imp, topic=topic)


async def test_nudge_when_our_point_turns_unanswered_or_is_ignored():
    bots, out, clock = make()
    bots.handle(C("c1", "A", "prop", "Tuition debt tops 45,000 pounds.", topic="graduate debt", checkable=False,
                  typ="value", imp=0.4))
    bots.handle(C("c9", "B", "opp", "x", checkable=False, typ="value", imp=0.4))
    bots.handle(ArgMapSnapshot(version=1, nodes=[node("c1", "prop", "open", topic="graduate debt")]))
    assert said(out) == []
    bots.handle(ArgMapSnapshot(version=2, nodes=[node("c1", "prop", "unanswered", topic="graduate debt")]))
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.claim_id) == ("nudge", "for", "B", "c1")
    assert b.text == "Still waiting on the graduate debt point."
    clock.t += 20
    bots.handle(ArgMapSnapshot(version=3, nodes=[node("c1", "prop", "unanswered", topic="graduate debt")]))
    bots.handle(ArgMapSnapshot(version=2, nodes=[node("c7", "opp", "unanswered")]))          # an older version
    assert len(said(out)) == 1
    bots.handle(ArgMapSnapshot(version=4, nodes=[node("c1", "prop", "unanswered"), node("c5", "opp", "open"),
                                                 node("c6", "prop", "open", topic="childcare costs")],
                               nonresponsive=[{"turn": 3, "side": "prop", "speaker": "A", "ignored": ["c5"]},
                                              {"turn": 4, "side": "opp", "speaker": "B", "ignored": ["c6", "c1"]}]))
    got = {(b.persona, b.target, b.claim_id) for b in said(out)[1:]}
    assert got == {("against", "A", "c5"), ("for", "B", "c6")}


async def test_no_nudge_from_a_turn_that_is_still_open():
    bots, out, clock = make()
    nodes = [node("c1", "prop", "open", topic="graduate debt")]
    open_turn = {"turn": 2, "side": "opp", "speaker": "B", "ignored": ["c1"], "final": False}
    bots.handle(ArgMapSnapshot(version=1, nodes=nodes, nonresponsive=[open_turn]))
    assert said(out) == []                                                      # B may still answer it
    bots.handle(ArgMapSnapshot(version=2, nodes=nodes, nonresponsive=[{**open_turn, "final": True}]))
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.claim_id) == ("nudge", "for", "B", "c1")


# ----------------------------------------------------------------------------- the debate record (Consistency)
OTHER_SIDE = {"prop": "opp", "opp": "prop"}


def K(kid, kind="self_contradiction", side="opp", spk="B", then="cut it by 6%", now="about 4%", topic="", **kw):
    """A Consistency finding: claim c2 (by `side`) against the earlier c1 (same side for a U-turn or a correction)."""
    kw.setdefault("earlier_side", side if kind in ("self_contradiction", "correction") else OTHER_SIDE[side])
    return Consistency(id=kid, kind=kind, claim_id="c2", earlier_claim_id="c1", side=side, speaker=spk, then=then,
                       now=now, topic=topic, explanation="The same side gave a different figure.", confidence=0.9, **kw)


UTURN_TEXT = "Earlier: 'cut it by 6%'. Now: 'about 4%'. Which is it?"


@pytest.mark.parametrize("spk,side,persona", [("B", "opp", "for"), ("A", "prop", "against")])
async def test_a_self_contradiction_gets_a_uturn_from_the_opposing_bot_quoting_both_claims(spk, side, persona):
    bots, out, clock = make(fast=FakeLLM(), check=FakeLLM(check=false_check()))
    bots.handle(K("k1", side=side, spk=spk, then="We would cut it by 6%.", now="about 4%"))
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.claim_id, b.sourced, b.evidence) == ("uturn", persona, spk, "c2", False, [])
    assert lines.OWN_SIDE[persona] != side
    assert b.text == "Earlier: 'We would cut it by 6%'. Now: 'about 4%'. Which is it?"   # both quotes verbatim
    assert "unsourced" not in b.text.lower() and len(b.text.split()) <= 18 and b.hold_s == lines.hold_s(b.text)
    assert bots.writer.fast.calls == [] and bots.writer.checker.calls == []             # a template: no LLM


def test_a_uturn_trims_long_quotes_with_an_ellipsis_keeping_their_figures():
    then = "Our plan would cut child poverty by 6% within two years of taking office, and it is fully costed."
    now = "In fact the figure we gave was about 4% over the parliament as a whole."
    text = lines.uturn(then, now)
    assert text == ("Earlier: '…child poverty by 6% within two…'. Now: '…gave was about 4% over the parliament…'. "
                    "Which is it?")
    assert len(text.split()) == lines.REBUT_MAX_WORDS
    assert lines.uturn("cut it by 6%", now) == \
        "Earlier: 'cut it by 6%'. Now: 'In fact the figure we gave was about 4%…'. Which is it?"   # the rest
    assert lines.excerpt("one two three four", 4) == "one two three four" and lines.excerpt("a b c", 0) == ""
    assert lines.excerpt("a b c d e f", 3) == "a b c…"
    assert lines.guard(" ".join(["word"] * 18), "uturn") and not lines.guard(" ".join(["word"] * 19), "uturn")
    assert lines.guard("Earlier: 'cut it by 6%'. Now: 'about 4%'. Which is it?", "uturn", "cut it by 6%") is None


def test_a_uturn_whose_quotes_fail_the_guards_drops_them():
    plain = "That's not what you said earlier. Which is it?"
    assert lines.uturn("That figure is false", "about 4%") == plain                      # a judgement word
    assert lines.uturn("Bob admits it fell by 6%", "about 4%", names=["Bob"]) == plain   # a debater's name
    assert lines.uturn("", "about 4%") == plain
    assert lines.uturn("cut it by 6%", "about 4%", used=[UTURN_TEXT]) == "You said 'cut it by 6%'. Now it's 'about 4%'?"
    assert lines.uturn("cut it by 6%", "about 4%", used=[UTURN_TEXT, "You said 'cut it by 6%'. Now it's 'about 4%'?",
                                                           plain]) is None


@pytest.mark.parametrize("spk,side,persona", [("B", "opp", "for"), ("A", "prop", "against")])
async def test_a_concession_gets_conceded_from_the_bot_whose_point_was_granted(spk, side, persona):
    bots, out, clock = make()
    bots.handle(K("k1", "concession", side=side, spk=spk, then="childcare costs rose", topic="childcare costs",
                  now="I'll grant that childcare costs rose"))
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.claim_id, b.sourced) == ("conceded", persona, spk, "c2", False)
    assert lines.OWN_SIDE[persona] == OTHER_SIDE[side]                                  # the conceded-to side
    assert b.text == "Noted: you just conceded the childcare costs point." and "unsourced" not in b.text.lower()


def test_conceded_wording():
    assert lines.conceded("cost") == "Noted: you just conceded the cost point."
    assert lines.conceded("") == lines.conceded("absurd claims") == "Noted: you just conceded our point."
    assert lines.conceded("The 6% target", "about 6%") == "Noted: you just conceded the 6% target point."
    assert lines.conceded("the 9% target", "about 6%") == "Noted: you just conceded our point."   # not their figure
    assert lines.conceded("cost", used=["Noted: you just conceded the cost point."]) == \
        "So you grant the cost point. Noted."


async def test_conceded_takes_the_earlier_claims_topic_when_the_finding_has_none():
    bots, out, clock = make()
    bots.handle(C("c1", "A", "prop", "Tuition debt tops 45,000 pounds.", topic="graduate debt", typ="value",
                  checkable=False, imp=0.3))
    bots.handle(K("k1", "concession", then="Tuition debt tops 45,000 pounds", now="that debt figure is right"))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.persona, b.target, b.text) == ("conceded", "for", "B", "Noted: you just conceded the graduate "
                                                                             "debt point.")


@pytest.mark.parametrize("finding", [
    K("k1", "agreement"), K("k1", "correction"),
    K("k1", "concession", earlier_side="opp"),                    # a concession to your own side: malformed
    K("k1", earlier_side="prop"),                                 # a U-turn against the other side: malformed
    K("k1", side=None, spk=None),                                 # no side, and no claim to take it from
])
async def test_agreements_corrections_and_malformed_findings_get_no_line(finding):
    bots, out, clock = make()
    bots.handle(finding)
    clock.t += 30
    bots.tick()
    assert said(out) == [] and bots.findings == {"k1"}


async def test_a_findings_side_and_speaker_default_to_its_claims():
    bots, out, clock = make()
    bots.handle(C("c2", "A", "prop", "It is about 4%.", typ="value", checkable=False, imp=0.3))
    bots.handle(K("k1", side=None, spk=None, earlier_side=None))
    await settle(bots)
    [b] = said(out)
    assert (b.kind, b.persona, b.target) == ("uturn", "against", "A")


async def test_one_line_per_consistency_finding(caplog):
    caplog.set_level(logging.INFO, logger="bots")
    bots, out, clock = make()
    bots.handle(K("k1"))
    clock.t += 30
    bots.handle(K("k1"))                                          # re-sent: the same finding
    bots.tick()
    assert [b.text for b in said(out)] == [UTURN_TEXT]
    bots.handle(K("k2"))                                          # another pair, the same words: never the same text
    clock.t += 30
    bots.tick()
    assert [b.text for b in said(out)] == [UTURN_TEXT, "You said 'cut it by 6%'. Now it's 'about 4%'?"]
    assert [r.getMessage() for r in caplog.records if r.name == "bots"] == [
        "bots: self_contradiction k1 by opp -> uturn queued", "bots: self_contradiction k2 by opp -> uturn queued"]


async def test_a_uturn_jumps_the_queue_but_never_cuts_a_bubble():
    config.BOTS_LINE_TTL_S = 60.0
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    bots.handle(C("c3", typ="causal", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    bots.handle(K("k1", "concession", then="wages rose", now="I accept wages rose", topic="wages"))
    bots.handle(K("k2"))
    assert [b.kind for b in said(out)] == ["followup"]            # For's bubble is still up
    assert sorted(b.kind for b in bots.pacer.queued("for")) == ["conceded", "followup", "uturn"]
    for _ in range(3):
        clock.t += 10
        bots.tick()
    assert [b.kind for b in said(out)] == ["followup", "uturn", "conceded", "followup"]


# ----------------------------------------------------------------------------- banter
async def test_banter_replies_once_per_gap_never_chains_and_is_guarded():
    config.BOTS_BANTER = True
    fast = FakeLLM(question="Which year is that from?")
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    first, reply = said(out)
    assert (first.persona, first.kind) == ("for", "followup")
    assert (reply.persona, reply.kind, reply.reply_to, reply.target) == ("against", "banter", first.id, None)
    assert reply.text == "Bold of you to fact-check with vibes."
    clock.t += 10                                                               # inside the 20 s gap
    bots.handle(C("c2", typ="value", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["followup", "banter", "followup"]
    assert sum(k == "banter" for k, _ in fast.calls) == 1                       # the banter line got no reply either
    clock.t += 15
    fast.line = "Steady on, that was rhetorical."
    bots.handle(C("c3", "A", "prop", typ="value", checkable=False, text="Fair pay is a moral duty."))
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)[3:]] == [("against", "followup"), ("for", "banter")]


async def test_banter_that_fails_its_guards_is_dropped_and_does_not_start_the_gap():
    config.BOTS_BANTER = True
    fast = FakeLLM(line="Ha, 99% of your lines are filler.")                   # a figure X's line lacks
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["followup"]
    fast.line = "Steady on, it was a question, not a closing speech."
    clock.t += 10
    bots.handle(C("c2", typ="value", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["followup", "followup", "banter"]


async def test_banter_goes_only_to_an_idle_bot():
    fast = FakeLLM()
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", "A", "prop", typ="value", checkable=False, text="Fair pay is a moral duty."))
    await settle(bots)                                                          # Against posts (banter still off)
    config.BOTS_BANTER = True
    clock.t += 2
    bots.handle(C("c2", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)] == [("against", "followup"), ("for", "followup")]
    assert not [k for k, _ in fast.calls if k == "banter"]                      # Against is still up: no call
    clock.t += 10
    bots.handle(C("c3", typ="causal", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)[2:]] == [("for", "followup"), ("against", "banter")]
    assert said(out)[3].reply_to == said(out)[2].id


def banters(fast) -> int:
    return sum(k == "banter" for k, _ in fast.calls)


@pytest.mark.parametrize("late", [False, True])
async def test_banter_waits_while_a_verdict_wait_is_in_flight(late):
    config.BOTS_BANTER, config.BOTS_VERDICT_WAIT_S = True, 5.0
    fast = FakeLLM()
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", "A", "prop", imp=0.3))                    # Against waits for c1's Verdict (no line after)
    bots.handle(C("c2", typ="value", checkable=False, text="Markets know best."))
    await asyncio.sleep(0.05)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup")] and banters(fast) == 0
    clock.t += 9 if late else 2                                   # the cue goes stale after BANTER_FRESH_S
    bots.handle(V("c1", label="unverifiable", evidence=[]))
    await settle(bots, 1.0)
    want = [("for", "followup")] + ([] if late else [("against", "banter")])
    assert [(b.persona, b.kind) for b in said(out)] == want
    if not late:
        assert said(out)[1].reply_to == said(out)[0].id


async def test_banter_waits_while_a_knowledge_check_is_in_flight():
    config.BOTS_BANTER = True
    fast = FakeLLM()
    bots, out, clock = make(fast=fast, check=FakeLLM(check=false_check(), delay=0.2))
    bots.handle(C("c1", "A", "prop", "Unemployment is 6% in the UK."))   # Against's check: 0.2 s
    bots.handle(C("c2", typ="value", checkable=False, text="Markets know best."))
    await asyncio.sleep(0.1)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup")] and banters(fast) == 0
    await settle(bots, 1.0)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup"), ("against", "challenge")]
    assert banters(fast) == 0                                     # then Against is busy and For is in its gap


async def test_banter_waits_while_a_real_line_is_queued_for_either_bot():
    config.BOTS_BANTER = True
    fast = FakeLLM()
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    bots.handle(C("c2", typ="causal", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup")] and bots.pacer.queued("for")
    assert banters(fast) == 0                                     # For's second line waits: Against stays quiet
    clock.t += 10
    bots.tick()                                                   # For's second line posts; now all is quiet
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup"), ("for", "followup"), ("against", "banter")]
    assert said(out)[2].reply_to == said(out)[1].id


async def test_banter_gap_is_20_s_across_both_bots():
    config.BOTS_BANTER = True
    fast = FakeLLM()
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup"), ("against", "banter")]
    clock.t += 19.9
    fast.line = "Steady on, that was rhetorical."
    bots.handle(C("c2", typ="causal", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["followup", "banter", "followup"] and banters(fast) == 1
    clock.t += 0.1                                                # 20 s after the first banter: the cue is fresh
    bots.tick()
    await settle(bots)
    assert [(b.persona, b.kind) for b in said(out)[3:]] == [("against", "banter")] and banters(fast) == 2


async def test_a_claim_that_comes_in_while_banter_is_written_stops_it_and_its_rebut_posts_at_once():
    config.BOTS_BANTER, config.BOTS_VERDICT_WAIT_S = True, 5.0
    fast = FakeLLM(delay=0.2)
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await asyncio.sleep(0.3)                                      # For's follow-up is up; Against's banter is coming
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup")] and banters(fast) == 1
    bots.handle(C("c2", "A", "prop", imp=0.3))                    # then Against waits for c2's Verdict
    await asyncio.sleep(0.2)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup")]   # the banter came back: not posted
    assert bots.last_banter == bots.banter_posted                 # and the gap was given back
    clock.t += 1
    bots.handle(V("c2"))
    await settle(bots, 1.0)
    assert [(b.persona, b.kind, b.ts) for b in said(out)][1:] == [("against", "rebut", T0 + 1)]   # no 10 s gap


async def test_a_banter_cut_short_by_a_claim_is_tried_again_once_all_is_quiet():
    config.BOTS_BANTER, config.BOTS_VERDICT_WAIT_S = True, 5.0
    fast = FakeLLM(delay=0.2)
    bots, out, clock = make(fast=fast)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await asyncio.sleep(0.3)                                      # Against's banter is on its way
    bots.handle(C("c2", "B", "opp", imp=0.3))                     # For now waits for c2's Verdict: not quiet
    await asyncio.sleep(0.2)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup")] and bots.cue is not None
    clock.t += 2
    bots.handle(V("c2", label="unverifiable", evidence=[]))       # c2 ends with no line: quiet again
    await settle(bots, 1.0)
    assert [(b.persona, b.kind) for b in said(out)] == [("for", "followup"), ("against", "banter")]
    assert said(out)[1].reply_to == said(out)[0].id and banters(fast) == 2


@pytest.mark.parametrize("how", ["claim", "line"])
async def test_a_queued_banter_goes_when_its_bot_gets_a_claim_or_a_line_and_gives_the_gap_back(how):
    config.BOTS_VERDICT_WAIT_S = 5.0
    fast = FakeLLM()
    bots, out, clock = make(fast=fast)
    bots.handle(C("c0", "A", "prop", typ="value", checkable=False, text="Fair pay is a moral duty."))
    await settle(bots)                                            # Against posts at T0 (banter still off)
    config.BOTS_BANTER = True
    clock.t += 8
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)                                            # For posts; Against is free within 3 s: banter
    assert [b.kind for b in bots.pacer.queued("against")] == ["banter"] and bots.last_banter == T0 + 8
    if how == "claim":
        bots.handle(C("c2", "A", "prop", imp=0.3))                # Against may have a rebut on its way
        bots.tick()
    else:
        bots.queue(bots.session, persona="against", text="Still waiting on the cost point.", kind="nudge", target="A")
    assert [b.kind for b in bots.pacer.queued("against")] in ([], ["nudge"])
    assert bots.last_banter == bots.banter_posted == -1e18        # nothing posted: the gap is free again
    clock.t += 2
    if how == "claim":
        bots.handle(V("c2"))
    await settle(bots, 1.0)
    bots.tick()
    want = "rebut" if how == "claim" else "nudge"
    assert [(b.persona, b.kind) for b in said(out)] == [("against", "followup"), ("for", "followup"), ("against", want)]


@pytest.mark.parametrize("how", ["pause", "session"])
async def test_a_pause_or_new_session_forgets_the_claims_in_flight(how):
    config.BOTS_VERDICT_WAIT_S = 5.0
    bots, out, clock = make()
    bots.handle(C("c1"))
    bots.handle(C("c2", typ="value", checkable=False, text="Markets know best."))
    assert bots.pending == {"c1", "c2"}                          # from the moment each is taken on
    await asyncio.sleep(0.01)
    assert bots.pending == {"c1"} and bots.cue is not None
    if how == "pause":
        bots.handle(Control(flag="bots", value=False))
        bots.handle(Control(flag="bots", value=True))
    else:
        bots.handle(SessionStart(motion="Another motion", speakers=SPEAKERS))
    assert bots.pending == set() and bots.cue is None
    await asyncio.sleep(0.01)
    assert not bots.tasks and bots.pending == set()


def test_pacer_drops_a_queued_banter_when_a_real_line_is_ready_first():
    p = Pacer(min_gap=10, ttl=30, banter_ttl=10)
    p.push(L(text="First?"), 0)
    p.tick(0)
    assert p.push(L(text="Quip.", kind="banter"), 1) and p.push(L("against", "Other quip.", kind="banter"), 1)
    assert p.push(L(text="Real one?"), 2)
    assert [x.text for x in p.queued("for")] == ["Real one?"]                  # For's banter is gone
    assert [x.text for x in p.queued("against")] == ["Other quip."]            # not the other bot's
    assert not p.push(L(text="Late quip.", kind="banter"), 3)                   # never queued behind a real line
    assert posted(p.tick(10)) == ["Real one?", "Other quip."] and posted(p.tick(30)) == []


async def test_banter_switch_off():
    fast = FakeLLM()
    bots, out, clock = make(fast=fast)                                          # BOTS_BANTER=False (fixture)
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["followup"] and [k for k, _ in fast.calls] == ["followup"]


# ----------------------------------------------------------------------------- switches and sessions
async def test_session_start_resets_everything_and_cancels_work():
    config.BOTS_VERDICT_WAIT_S = 5.0
    bots, out, clock = make()
    bots.handle(C("c0", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    bots.handle(C("c1"))                                                        # waiting on its Verdict
    bots.handle(C("c2", typ="value", checkable=False, text="Regulation always backfires."))   # queued: gap
    await asyncio.sleep(0.01)
    assert bots.tasks and bots.pacer.queued("for")
    bots.handle(SessionStart(motion="Another motion", speakers=SPEAKERS))
    await asyncio.sleep(0.01)
    assert not bots.tasks and bots.pacer.queued("for") == [] and bots.motion == "Another motion"
    bots.handle(V("c1"))
    bots.tick()
    assert len(said(out)) == 1
    bots.handle(C("c0", typ="value", checkable=False, text="Markets know best."))   # a new session: same text again
    await settle(bots)
    assert [b.text for b in said(out)] == ["Why should anyone accept that?"] * 2


@pytest.mark.parametrize("flag", ["bots", "mute"])
async def test_bots_false_or_mute_stops_posting_and_drops_queues(flag):
    off, on = (False, True) if flag == "bots" else (True, False)
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    bots.handle(C("c2", typ="value", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    first = said(out)[0]
    bots.handle(Control(flag=flag, value=off))
    [talk] = [e for e in out if e.event == "BotTalk"]                            # the open mouth closes now
    assert talk.line_id == first.id and bots.pacer.queued("for") == []
    bots.handle(C("c3", typ="value", checkable=False, text="Unions protect nobody."))
    clock.t += 60
    bots.tick()
    assert len(said(out)) == 1 and not bots.tasks
    bots.handle(Control(flag=flag, value=on))
    bots.handle(C("c4", typ="value", checkable=False, text="Unions protect nobody."))
    await settle(bots)
    assert len(said(out)) == 2


@pytest.mark.parametrize("how", ["live", "session"])
async def test_the_final_screen_pauses_the_bots_until_live_or_a_new_session(how):
    config.BOTS_VERDICT_WAIT_S = 5.0
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    bots.handle(C("c2", typ="value", checkable=False, text="Regulation always backfires."))
    await settle(bots)
    first = said(out)[0]
    bots.handle(C("c3"))                                                        # waiting for its Verdict
    await asyncio.sleep(0.01)
    assert bots.tasks and bots.pending == {"c3"} and bots.pacer.queued("for")
    bots.handle(Control(flag="screen", value="final"))
    [talk] = [e for e in out if e.event == "BotTalk"]                            # the open mouth closes now
    assert talk.line_id == first.id and bots.pacer.queued("for") == [] and bots.pending == set()
    assert bots.paused and bots.enabled and not bots.muted                      # the operator's flags untouched
    await asyncio.sleep(0.01)
    assert not bots.tasks
    bots.handle(K("k1"))                                          # the summary's catch-up finds a U-turn
    bots.handle(C("c4", typ="value", checkable=False, text="Unions protect nobody."))
    bots.handle(V("c3"))
    clock.t += 60
    bots.tick()
    assert len(said(out)) == 1 and not bots.tasks
    if how == "live":
        bots.handle(Control(flag="screen", value="live"))
    else:
        bots.handle(SessionStart(motion="Another motion", speakers=SPEAKERS))
    assert not bots.paused and not bots.pacer.paused
    bots.handle(K("k1"))                                          # live: seen at the final screen, never posted late
    bots.handle(C("c5", typ="value", checkable=False, text="Tariffs protect jobs."))
    await settle(bots)
    assert [b.kind for b in said(out)] == ["followup"] + (["followup"] if how == "live" else ["uturn"])


async def test_leaving_the_final_screen_keeps_the_operators_bots_switch():
    bots, out, clock = make()
    bots.handle(Control(flag="bots", value=False))
    bots.handle(Control(flag="screen", value="final"))
    bots.handle(Control(flag="screen", value="live"))
    assert bots.paused and not bots.enabled
    bots.handle(Control(flag="screen", value="final"))
    bots.handle(Control(flag="bots", value=True))
    assert bots.paused and bots.final                                           # still the final screen
    bots.handle(SessionStart(motion="Another motion", speakers=SPEAKERS))
    assert not bots.paused and not bots.pacer.paused


async def test_operator_switch_set_before_the_bots_start_is_honoured(monkeypatch):
    monkeypatch.setattr(config, "FLAGS", {"bots": False})
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    assert said(out) == []


async def test_restore_after_a_crash_remembers_what_was_said():
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    history = [SessionStart(motion="m", speakers=SPEAKERS, ts=T0), C("c1", typ="value", checkable=False),
               *said(out), ArgMapSnapshot(version=5, nodes=[node("c5", "prop", "unanswered")])]
    again, out2, clock2 = make()
    again.restore(history)
    assert again.claims.keys() == {"c1"} and again.statuses == {"c5": "unanswered"}
    assert again.pacer.taken(BotLine(persona="for", text=said(out)[0].text))    # already said
    again.handle(C("c2", typ="value", checkable=False, text="Regulation always backfires."))
    await settle(again)
    assert said(out2) == [] and again.pacer.queued("for")                      # the gap runs from the restored line
    clock2.t += 10
    again.tick()
    assert [b.text for b in said(out2)] == ["Says who, and why?"]
    again.handle(ArgMapSnapshot(version=6, nodes=[node("c5", "prop", "unanswered")]))
    assert len(said(out2)) == 1                                                 # already unanswered: no nudge


async def test_restore_remembers_the_findings_and_the_final_screen():
    bots, out, clock = make()
    bots.handle(K("k1"))
    [b] = said(out)
    start = SessionStart(motion="m", speakers=SPEAKERS, ts=T0)
    history = [start, K("k1"), b, K("k2", "agreement"), Control(flag="screen", value="final")]
    again, out2, clock2 = make()
    again.restore(history)
    assert again.findings == {"k1", "k2"} and again.paused and again.pacer.paused and again.enabled
    again.handle(Control(flag="screen", value="live"))
    again.handle(K("k1"))                                         # answered before the crash
    clock2.t += 30
    again.tick()
    assert said(out2) == []
    again.handle(K("k3"))                                         # a new finding, the same words: not the same text
    assert [x.text for x in said(out2)] == ["You said 'cut it by 6%'. Now it's 'about 4%'?"]
    for tail, paused in (([Control(flag="screen", value="live")], False), ([start], False), ([], True)):
        again, out2, clock2 = make()
        again.restore(history + tail)
        assert (again.paused, again.pacer.paused) == (paused, paused)


async def test_restore_closes_a_mouth_the_crash_left_open():
    bots, out, clock = make()
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    bots.handle(C("c2", "A", "prop", typ="value", checkable=False, text="Fair pay is a moral duty."))
    await settle(bots)
    closed, cut = said(out)                                                     # For, then Against
    clock.t += closed.hold_s
    bots.tick()
    talks = [e for e in out if e.event == "BotTalk"]
    history = [SessionStart(motion="m", speakers=SPEAKERS, ts=T0), *said(out), *talks]
    assert [t.line_id for t in talks] == [closed.id, cut.id]
    history.remove(talks[1])                                                    # the crash came before cut's off
    again, out2, clock2 = make()
    clock2.t = clock.t
    again.restore(history)
    assert [e for e in out2 if e.event == "BotTalk"] == []                      # silent until the next tick
    again.tick()
    [off] = [e for e in out2 if e.event == "BotTalk"]
    assert (off.persona, off.line_id, off.on) == ("against", cut.id, False)


# ----------------------------------------------------------------------------- writer
async def test_writer_calls_are_bounded_and_never_raise(monkeypatch):
    monkeypatch.setattr(writer_mod, "FAST_TIMEOUT_S", 0.6)
    slow = FakeLLM(delay=5)
    w = Writer(slow, None, concurrency=1)
    t0 = asyncio.get_running_loop().time()
    assert await asyncio.gather(w.followup(C("c1")), w.followup(C("c2"))) == ["", ""]
    assert asyncio.get_running_loop().time() - t0 < 2 and len(slow.calls) == 1   # the second never got a slot
    assert await Writer(None, None).check(C("c1")) is None and Writer().name == "templates only (no LLM)"
    got = await Writer(None, FakeLLM(check=false_check("90%"))).check(C("c1"))
    assert got.likely_false and got.confidence == 0.9 and got.correction == "it was nearer 4%"
    got = await Writer(None, FakeLLM(check=misleading_check(0.8))).check(C("c1"))
    assert (got.assessment, got.context, got.likely_false) == \
        ("misleading", "that's against a modelled baseline", False)
    got = await Writer(None, FakeLLM(check=check_answer("Surprising_True", 0.9, "Fine."))).check(C("c1"))
    assert (got.assessment, got.line) == ("surprising_true", "Fine.")
    got = await Writer(None, FakeLLM(check=check_answer("half_true", 0.99))).check(C("c1"))
    assert got.assessment == "unsure"


def test_make_writer_uses_brains_client_with_the_bots_models(monkeypatch):
    client = object()
    monkeypatch.setattr(writer_mod, "make_llm", lambda: AnthropicLLM(client, model="claude-haiku-5-5"))
    w = writer_mod.make_writer()
    assert w.fast.client is client and w.checker.client is client
    assert (w.fast.model, w.checker.model) == (config.BOTS_MODEL, config.BOTS_CHECK_MODEL)
    other = FakeLLM()
    monkeypatch.setattr(writer_mod, "make_llm", lambda: other)
    w = writer_mod.make_writer()
    assert w.fast is other and w.checker is other
    monkeypatch.setattr(writer_mod, "make_llm", lambda: None)
    assert writer_mod.make_writer().fast is None


# ----------------------------------------------------------------------------- the module on a real bus
async def wait_for(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if (got := pred()):
            return got
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_module_on_the_bus_answers_and_survives_a_failing_handler(monkeypatch):
    from app.bots import run as bots_run
    monkeypatch.setattr(writer_mod, "make_writer", lambda: Writer(None, None))
    monkeypatch.setattr(bots_run.Bots, "on_snapshot", lambda self, ev: 1 / 0)
    bus = Bus()
    task = asyncio.create_task(bots_run.run(bus))
    try:
        await wait_for(lambda: any(e.event == "ModuleStatus" and e.module == "bots" for e in bus.history))
        bus.publish(SessionStart(motion="m", speakers=SPEAKERS))
        bus.publish(ArgMapSnapshot(version=1))                                  # raises inside the module
        bus.publish(C("c1"), src="brain")
        bus.publish(V("c1", numeric=Numeric(claimed=6, actual=4.2, unit="%")), src="evidence")
        b = await wait_for(lambda: next((e for e in bus.history if e.event == "BotLine"), None))
        assert (b.src, b.kind, b.persona, b.sourced) == ("bots", "rebut", "for", True)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_module_on_the_bus_heckles_a_u_turn_and_pauses_at_the_final_screen(monkeypatch):
    from app.bots import run as bots_run
    monkeypatch.setattr(writer_mod, "make_writer", lambda: Writer(None, None))
    bus = Bus()
    task = asyncio.create_task(bots_run.run(bus))

    def lines_() -> list[BotLine]:
        return [e for e in bus.history if e.event == "BotLine"]

    try:
        await wait_for(lambda: any(e.event == "ModuleStatus" and e.module == "bots" for e in bus.history))
        bus.publish(SessionStart(motion="m", speakers=SPEAKERS))
        bus.publish(K("k1"), src="brain")
        [b] = await wait_for(lines_)
        assert (b.src, b.kind, b.persona, b.target, b.text) == ("bots", "uturn", "for", "B", UTURN_TEXT)
        bus.publish(Control(flag="screen", value="final"))
        bus.publish(K("k2", side="prop", spk="A"), src="brain")
        await asyncio.sleep(0.1)
        assert len(lines_()) == 1 and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_main_starts_the_bots_only_with_bots_real(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as mainmod
    assert mainmod.resolve("bots", "real") == "app.bots.run:run"
    assert "bots" not in {r for p in mainmod.PROFILES.values() for r in p}
    monkeypatch.setattr(config, "LLM_PROVIDER", "heuristic")                    # never the network
    for k in ("EARS", "BRAIN", "EVIDENCE", "FACE"):
        monkeypatch.setenv(k, "off")
    monkeypatch.delenv("BOTS", raising=False)
    for env, started in ((None, False), ("real", True)):
        if env:
            monkeypatch.setenv("BOTS", env)
        with TestClient(mainmod.build_app("mock", log_dir=tmp_path)) as c:
            assert ("bots" in c.get("/health").json()["modules"]) is started
            if started:
                for _ in range(100):
                    hist = c.get("/debug/history").json()
                    if any(e["event"] == "ModuleStatus" and e["module"] == "bots" and "llm" in e["detail"]
                           for e in hist):
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError("bots never came up")
            else:
                hist = c.get("/debug/history").json()
                assert not any(e["event"] == "ModuleStatus" and e["module"] == "bots" for e in hist)



def test_tunable_defaults():
    """The shipped defaults (a fresh interpreter: no .env, no BOTS_* overrides)."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOTS_")} | {"DEBATE_REF_NO_DOTENV": "1"}
    code = ("from app import config as c; "
            "print(c.BOTS_CHECK_MIN_CONF, c.BOTS_MISLEADING_MIN_CONF, c.BOTS_BANTER_GAP_S, c.BOTS_MIN_GAP_S)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env,
                         cwd=Path(__file__).resolve().parent.parent, check=True).stdout
    assert out.split() == ["0.8", "0.6", "20.0", "10.0"]


# ----------------------------------------------------------------------------- live speech (debug/bots regressions)
@pytest.mark.parametrize("deep,kind", [(dict(), "rebut"), (dict(label="supported"), "followup")])
async def test_a_deep_verdict_that_replaces_an_unverifiable_one_while_the_check_runs_still_wins(deep, kind):
    """Evidence publishes a second, better Verdict for the same claim when its deep check does better (a timeout
    or no source first): the bot must use it, not the first one."""
    bots, out, clock = make(check=FakeLLM(check=false_check(), delay=0.3))
    bots.handle(C("c1"))
    bots.handle(V("c1", label="unverifiable", evidence=[]))     # the fast check found nothing (or ran out of time)
    await asyncio.sleep(0.1)
    bots.handle(V("c1", **deep))                                # the deep check, while Claude's check still runs
    await settle(bots, 1.0)
    [b] = said(out)
    assert b.kind == kind and b.sourced == (kind == "rebut")
    if kind == "followup":
        assert b.text in lines.GRANTED


@pytest.mark.parametrize("name,question", [
    ("Anne-Marie Duval", "Anne-Marie, which baseline year?"),
    ("Siobhán Kelly", "Siobhán, which baseline year?"),
    ("Dara O'Neill", "O'Neill, which baseline year?"),
    ("Zoë Kent", "Zoë, which baseline year?"),
])
async def test_a_hyphenated_accented_or_apostrophe_name_never_reaches_a_bubble(name, question):
    bots, out, clock = make(fast=FakeLLM(question=question))
    bots.handle(SessionStart(motion="M", speakers={"A": SpeakerInfo(name="Alice Archer", side="prop"),
                                                   "B": SpeakerInfo(name=name, side="opp")}))
    bots.handle(C("c1", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)
    [b] = said(out)
    assert b.text == "Why should anyone accept that?"                       # the template, not the named question


async def test_the_other_mics_copy_of_a_claim_is_not_heckled_as_the_other_sides():
    """Mic bleed the brain's guard missed (the phone's Wi-Fi lag, a different segment): Prop's words come back as an
    Opp claim. Heckling Opp for them is heckling the wrong debater."""
    bots, out, clock = make()
    bots.handle(C("c1", "A", "prop", "Raising wages always destroys small businesses.", typ="causal", checkable=False))
    clock.t += 2
    bots.handle(C("c2", "B", "opp", "raising wages always destroys small businesses", typ="causal", checkable=False))
    bots.handle(C("c3", "B", "opp", "Higher wages pay for themselves in productivity.", typ="causal",
                  checkable=False))                                     # a real Opp claim at the same moment
    await settle(bots)
    clock.t += 10
    bots.tick()
    assert sorted((b.persona, b.claim_id) for b in said(out)) == [("against", "c1"), ("for", "c3")]


def test_a_nudge_topic_that_starts_with_an_article_is_not_doubled():
    assert lines.nudge("the minimum wage") == "Still waiting on the minimum wage point."
    assert lines.nudge("The cost of living") == "Still waiting on the cost of living point."


def test_a_template_that_differs_from_a_said_line_only_in_punctuation_gives_way_to_the_next():
    assert lines.followup(C("c1"), ["source for that 6%."]) == "Where does that 6% come from?"
    assert lines.nudge("graduate debt", used=["Still waiting on the graduate debt point?"]) == \
        "Nothing on graduate debt, then?"


async def test_two_claims_queued_behind_a_bubble_with_the_llm_down_get_different_templates():
    bots, out, clock = make()                                                  # no LLM: templates only
    bots.handle(C("c0", typ="value", checkable=False, text="Markets know best."))
    await settle(bots)                                                         # the For bot's bubble is up
    bots.handle(C("c1", typ="fact", checkable=False, text="Nobody ever lost a job to a pay rise."))
    bots.handle(C("c2", typ="fact", checkable=False, text="Small firms all back the rise."))
    await settle(bots)
    for _ in range(2):
        clock.t += 10
        bots.tick()
    assert sorted((b.claim_id, b.text) for b in said(out)[1:]) == [("c1", "Says who?"), ("c2", "Source, please?")]


@pytest.mark.parametrize("text,fig", [
    ("Unemployment is 6 %.", "6%"),                          # STT spacing: the % is still part of it
    ("Unemployment is 6.%", "6%"),                           # a stray point before the %
    ("Prices are up 20-30% this year.", "20-30%"),           # a range is one figure, not its first number
    ("Between 20 to 30% of firms would close.", "20 to 30%"),
    ("Roughly 1/3 of workers are affected.", "1/3"),
    ("From 2010 to 2019 wages fell 5%.", "5%"),              # a span of years is not a figure
    ("Wages rose in 2019.", ""),
    ("It costs £3bn a year.", "£3bn"),
    ("Unemployment is 6% in the UK.", "6%"),
])
def test_the_figure_a_template_quotes_survives_stt_spacing_ranges_and_fractions(text, fig):
    assert lines.figure(text) == fig
    line = lines.followup(C("c1", text=text))
    assert line == (f"Source for that {fig}?" if fig else "Source for that number?")
