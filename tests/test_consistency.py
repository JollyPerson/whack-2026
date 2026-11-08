"""Consistency checker + debate summary: guards, caching, the heuristic, the argument map, the Director, the brain
wiring, the mock and the test-haiku profile. No network, no keys: every model is a fake."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from app import config
from app.brain import consistency as cons
from app.brain.argmap import ArgMap
from app.brain.consistency import ConsistencyChecker, Item, candidates, guard, heuristic, turn_blocks
from app.brain.director import Director
from app.schemas import Claim, Consistency, Control, SessionStart, Utterance, Verdict
from tests.test_run import Brain, Clock, brain  # noqa: F401  (the real-bus brain fixture)

MOTION = "This house would ban cars from city centres"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consistency_debate.jsonl"
T0 = 1_762_532_400.0


class FakeLLM:
    """complete_json returns the queued answers in order (the last repeats); records what it was sent."""
    name, model, supports_blocks = "fake", "fake-haiku", True

    def __init__(self, *answers, summary=None):
        self.answers, self.calls, self.users, self.summary_answer = list(answers), 0, [], summary
        self.usage = {"calls": 0}
        self.checks = 0

    async def complete_json(self, system, user, schema, timeout, max_tokens=400):
        self.calls += 1
        self.users.append(user)
        if "agreed" in schema["properties"]:          # the summary call
            if isinstance(self.summary_answer, Exception):
                raise self.summary_answer
            return self.summary_answer or {"agreed": [], "disputed": []}
        self.checks += 1
        a = self.answers[min(self.checks - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return a


def debate(turns: list[tuple[str, list[str]]]) -> ArgMap:
    """An argument map with one claim per line: claim ids c1.., utterances u1.., turns as given."""
    amap = ArgMap()
    amap.reset(SessionStart(motion=MOTION, speakers={"A": {"side": "prop"}, "B": {"side": "opp"}}))
    n, t = 0, T0
    for spk, lines in turns:
        for line in lines:
            n += 1
            t += 3
            amap.on_utterance(Utterance(id=f"u{n}", speaker=spk, text=line, t_start=t - 2, t_end=t, ts=t))
            amap.add_claim(Claim(id=f"c{n}", utterance_id=f"u{n}", speaker=spk, text=line, importance=0.67, ts=t))
    return amap


def fixture_turns() -> list[tuple[str, list[str]]]:
    turns: list = []
    for line in FIXTURE.read_text().splitlines():
        e = json.loads(line)
        if e["event"] == "Utterance":
            if turns and turns[-1][0] == e["speaker"]:
                turns[-1][1].append(e["text"])
            else:
                turns.append((e["speaker"], [e["text"]]))
    return turns


def finding(kind, cid, eid, now, then, conf=.9):
    return {"kind": kind, "claim_id": cid, "earlier_claim_id": eid, "now": now, "then": then,
            "explanation": "x", "topic": "cost", "confidence": conf}


# A: c1 cost nothing / c2 short trips | B: c3 70% by car / c4 grant short trips | A: c5 two billion
SMALL = [("A", ["The scheme would cost the taxpayer almost nothing.", "Most car trips into town are under two miles."]),
         ("B", ["Nearly 70 percent of shoppers arrive by car.", "I'll grant that most car trips into town are short."]),
         ("A", ["The scheme would cost around two billion pounds."]),
         ("B", ["Even if trips are short, shops still lose trade."])]


def checker(amap, llm=None, published=None, **kw):
    out = published if published is not None else []
    return ConsistencyChecker(amap, out.append, llm, delay_s=0, min_conf=.7, **kw), out


async def run_all(c: ConsistencyChecker) -> None:
    for t in c.amap.turns:
        if t.claims:
            await c.check_turn(t.idx)


# ----------------------------------------------------------------------------- guards
def items():
    amap = debate(SMALL)
    return {i.id: i for i in cons.items_from_map(amap)}


@pytest.mark.parametrize("raw, why", [
    (finding("self_contradiction", "c5", "c9", "two billion", "almost nothing"), "unknown earlier id"),
    (finding("self_contradiction", "c1", "c5", "almost nothing", "two billion"), "earlier is later"),
    (finding("concession", "c5", "c1", "two billion", "almost nothing"), "concession to your own side"),
    (finding("self_contradiction", "c4", "c2", "short", "under two miles"), "U-turn across sides"),
    (finding("self_contradiction", "c5", "c1", "costs two billion", "almost nothing"), "paraphrased now"),
    (finding("self_contradiction", "c5", "c1", "two billion", "next to nothing"), "paraphrased then"),
    (finding("self_contradiction", "c5", "c1", "two billion", "almost nothing", conf=.5), "low confidence"),
    (finding("teleport", "c5", "c1", "two billion", "almost nothing"), "unknown kind"),
    ({"kind": "concession", "claim_id": None}, "malformed"),
])
def test_guard_drops_findings_it_cannot_trust(raw, why):
    by_id = items()
    assert guard(raw, by_id, {"c5", "c3", "c4", "c6"}, set(), .7) is None, why


def test_guard_keeps_a_good_finding_and_quotes_survive_typography():
    k = guard(finding("self_contradiction", "c5", "c1", "around two billion pounds", "cost the taxpayer almost nothing"),
              items(), {"c5"}, set(), .7)
    assert k.kind == "self_contradiction" and k.side == "prop" and k.earlier_side == "prop" and k.turn == 2
    assert guard(finding("concession", "c4", "c2", "I’ll grant", "under two miles"), items(), {"c4"}, set(), .7)


def test_new_claim_must_be_in_the_turn_being_checked():
    assert guard(finding("self_contradiction", "c5", "c1", "two billion", "almost nothing"), items(), {"c3"}, set(), .7) is None


def test_hypothetical_is_never_a_concession():
    raw = finding("concession", "c6", "c4", "Even if trips are short", "most car trips into town are short")
    assert guard(raw, items(), {"c6"}, set(), .7) is None


def test_figure_changed_after_the_referee_is_a_correction_not_a_u_turn():
    k = guard(finding("self_contradiction", "c5", "c1", "two billion", "almost nothing"), items(), {"c5"}, {"c1"}, .7)
    assert k.kind == "correction"


# ----------------------------------------------------------------------------- candidates + heuristic
def test_candidates_ignore_the_motion_words_everyone_shares():
    amap = debate([("A", ["Banning cars from city centres would cost the taxpayer almost nothing."]),
                   ("A", ["Most car trips into our city centres are under two miles."])])
    its = cons.items_from_map(amap)
    ignore = cons.content(MOTION)
    assert heuristic(its[1], candidates(its[1], its[:1], ignore), ignore) == []    # sharing "city centres" isn't a U-turn


def test_heuristic_finds_the_planted_cases_in_the_fixture_and_not_the_even_if():
    amap = debate(fixture_turns())
    c, out = checker(amap)
    asyncio.run(run_all(c))
    got = {(k.kind, k.side) for k in out}
    texts = {k.kind: [] for k in out}
    for k in out:
        texts[k.kind].append(amap.nodes[k.claim_id].claim.text)
    assert ("concession", "opp") in got and ("agreement", "opp") in got
    assert not any(t.startswith("Even if") for t in sum(texts.values(), []))
    assert any("NHS" in t for t in texts.get("concession", []))
    assert c.stats["calls"] == 0 and c.stats["heuristic"] >= 1


# ----------------------------------------------------------------------------- the check
async def test_model_finding_is_published_folded_into_the_map_and_noticed():
    amap = debate(SMALL)
    llm = FakeLLM({"findings": [finding("concession", "c4", "c2", "I'll grant", "under two miles", .85)]},
                  {"findings": [finding("self_contradiction", "c5", "c1", "two billion", "almost nothing", .9)]},
                  {"findings": []})
    c, out = checker(amap, llm)
    await run_all(c)
    assert [k.kind for k in out] == ["concession", "self_contradiction"]
    assert amap.nodes["c2"].status == "conceded"                          # the concession is a supports link now
    snap = amap.snapshot()
    assert snap.score["prop"].self_contradictions == 1 and snap.score["opp"].self_contradictions == 0
    kinds = [(n.kind, n.detail["kind"]) for n in amap.drain_notices() if n.kind == "consistency"]
    assert kinds == [("consistency", "concession"), ("consistency", "self_contradiction")]


async def test_turn_without_candidates_costs_no_call():
    amap = debate([("A", ["Air pollution kills forty thousand people a year."]),
                   ("B", ["Shopkeepers in Leeds report quieter Saturdays."])])
    llm = FakeLLM({"findings": []})
    c, _ = checker(amap, llm)
    await run_all(c)
    assert llm.calls == 0 and c.stats["skipped_no_candidates"] == 1


async def test_model_failure_falls_back_to_the_heuristic():
    amap = debate([("A", ["Cleaner air would save the NHS money within a decade."]),
                   ("B", ["My opponent is right that cleaner air would save the NHS money."])])
    c, out = checker(amap, FakeLLM(TimeoutError("slow")))
    await run_all(c)
    assert [k.kind for k in out] == ["concession"] and c.stats["heuristic"] == 1


async def test_a_pair_is_reported_once_even_if_the_model_repeats_it():
    amap = debate(SMALL)
    same = finding("self_contradiction", "c5", "c1", "two billion", "almost nothing")
    c, out = checker(amap, FakeLLM({"findings": [same, same]}))
    await c.check_turn(2)
    await c.check_turn(2)                                                 # already checked: no second call
    assert len(out) == 1 and c.llm.calls == 1


async def test_restored_findings_are_never_paid_for_again():
    amap = debate(SMALL)
    k = Consistency(id="k-old", kind="self_contradiction", claim_id="c5", earlier_claim_id="c1", side="prop",
                    now="two billion", then="almost nothing", confidence=.9, turn=2)
    llm = FakeLLM({"findings": [finding("self_contradiction", "c5", "c1", "two billion", "almost nothing")]})
    c, out = checker(amap, llm)
    c.apply(k)
    c.apply(k)
    c.restored()
    await run_all(c)                                                      # only the still-open last turn is asked
    assert out == [] and llm.calls == 1 and len(c.findings) == 1


def test_due_waits_for_late_claims_and_never_hands_out_a_turn_twice():
    amap = debate(SMALL)
    c = ConsistencyChecker(amap, lambda e: None, None, delay_s=3.0)
    assert c.due(100.0) == []                                             # just noticed closed: wait
    assert c.due(103.5) == [0, 1, 2]                                      # turn 3 is still open
    assert c.due(110.0) == []                                             # queued: not again
    assert c.due(110.0, include_open=True) == [3]


def test_blocks_are_append_only_so_the_cached_prefix_keeps_matching():
    its = cons.items_from_map(debate(SMALL))
    first = turn_blocks([i for i in its if i.turn < 2], [i for i in its if i.turn == 2], {})
    second = turn_blocks([i for i in its if i.turn < 3], [i for i in its if i.turn == 3], {})
    assert [b["text"] for b in first[:-1]] == [b["text"] for b in second[:len(first) - 1]]
    assert "cache_control" in second[-2] and "cache_control" not in second[0]
    assert sum("cache_control" in b for b in second) == 1
    assert second[-1]["text"].startswith("NEW TURN")


async def test_model_gets_blocks_and_the_motion_first():
    amap = debate(SMALL)
    llm = FakeLLM({"findings": []})
    c, _ = checker(amap, llm)
    await c.check_turn(2)
    blocks = llm.users[0]
    assert isinstance(blocks, list) and blocks[0]["text"].startswith("MOTION: " + MOTION)


async def test_plain_text_for_a_model_without_blocks():
    llm = FakeLLM({"findings": []})
    llm.supports_blocks = False
    c, _ = checker(debate(SMALL), llm)
    await c.check_turn(2)
    assert isinstance(llm.users[0], str) and "NEW TURN" in llm.users[0]


# ----------------------------------------------------------------------------- summary
async def test_summary_is_cached_per_map_version_and_code_decides_the_lean():
    amap = debate(SMALL)
    amap.on_verdict(Verdict(claim_id="c1", label="contradicted", confidence=.9, evidence=["https://ons.gov.uk/x"]))
    amap.on_verdict(Verdict(claim_id="c3", label="supported", confidence=.9, evidence=["https://ons.gov.uk/y"]))
    llm = FakeLLM({"findings": []},
                  summary={"agreed": [{"statement": "Most car trips into town are short.", "claim_ids": ["c2", "c4", "zz"]}],
                           "disputed": [{"issue": "Cost and shops", "prop": "Cheap", "opp": "Shops lose",
                                         "claim_ids": ["c1", "c3"], "evidence_leans": "prop"}]})
    c, _ = checker(amap, llm)
    s1 = await c.summary(0.0)
    calls = llm.calls
    s2 = await c.summary(1.0)
    assert s2 is s1 and llm.calls == calls                                # second press: free
    assert s1.agreed[0].claim_ids == ["c2", "c4"]                         # unknown ids dropped
    assert s1.disputed[0].evidence_leans == "opp"                         # from verdicts, not the model's word
    assert set(s1.score) == {"prop", "opp"} and s1.motion == MOTION and s1.source == "llm"
    amap.on_verdict(Verdict(claim_id="c5", label="supported", confidence=.9, evidence=["https://ons.gov.uk/z"]))
    await c.summary(2.0)
    assert llm.calls == calls + 1                                         # the debate moved on: asked again


async def test_summary_without_a_model_still_has_agreed_and_disputed_points():
    amap = debate(fixture_turns())
    c, _ = checker(amap)
    s = await c.summary(0.0)
    assert s.source == "heuristic" and s.agreed and s.disputed
    assert s.concessions["opp"] >= 1


async def test_summary_survives_a_failing_model():
    c, _ = checker(debate(SMALL), FakeLLM(RuntimeError("down"), summary=RuntimeError("down")))
    assert (await c.summary(0.0)).source == "heuristic"


# ----------------------------------------------------------------------------- the Director
def director_with(claims: list[Claim]) -> Director:
    d = Director()
    d.reset(SessionStart(motion=MOTION, speakers={"A": {"side": "prop"}, "B": {"side": "opp"}}))
    for cl in claims:
        d.handle(cl, T0)
    return d


def notice(kind="self_contradiction", conf=.9):
    from app.brain.types import Notice
    return Notice("consistency", "A", T0, "c5", {"kind": kind, "earlier_claim_id": "c1", "topic": "scheme cost",
                                                  "side": "prop", "confidence": conf, "id": "k1"})


CL5 = Claim(id="c5", speaker="A", side="prop", text="It would cost two billion.", importance=1.0, t_end=T0, ts=T0)


def test_consistency_is_screen_only_unless_voiced(monkeypatch):
    monkeypatch.setattr(config, "CONSISTENCY_VOICE", False)
    d = director_with([CL5])
    out = d.notice(notice(), T0)
    assert [(e.action, e.reason) for e in out if e.event == "DirectorDecision"] == [("skipped", "dashboard_only")]


def test_voiced_u_turn_is_a_candidate_with_its_line(monkeypatch):
    monkeypatch.setattr(config, "CONSISTENCY_VOICE", True)
    d = director_with([CL5])
    dec = [e for e in d.notice(notice(), T0) if e.event == "DirectorDecision"]
    assert dec[0].action == "candidate" and "contradicts your earlier point" in dec[0].text
    assert "scheme cost" in dec[0].text
    agree = [e for e in d.notice(notice("agreement"), T0) if e.event == "DirectorDecision"]
    assert agree == [] or agree[0].reason in ("not_voiced", "duplicate")


# ----------------------------------------------------------------------------- the brain, on a real bus
async def test_brain_checks_closed_turns_and_publishes_the_summary_on_final(brain, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "CONSISTENCY_DELAY_S", 0.5, raising=False)
    monkeypatch.setenv("CONSISTENCY_DELAY_S", "0.5")
    await brain.start()
    brain.session()
    await brain.step()
    t, n = brain.now(), 0
    for spk, lines in [("A", ["Cleaner air would save the NHS money within a decade."]),
                       ("B", ["My opponent is right that cleaner air would save the NHS money."]),
                       ("A", ["Thank you, and that is exactly my point about the health service."])]:
        for line in lines:
            n += 1
            brain.pub(Utterance(id=f"u{n}", speaker=spk, text=line, t_start=t, t_end=t + 2, ts=t + 2))
            await brain.step()
            brain.pub(Claim(id=f"x{n}", utterance_id=f"u{n}", speaker=spk, text=line, importance=.8, t_end=t + 2),
                      src="ext")
            await brain.step(3)
            t += 3
    await brain.step(2)
    ks = brain.of("Consistency")
    assert ks and {(k.kind, k.side, k.earlier_side) for k in ks} == {("concession", "opp", "prop")}
    assert len({(k.claim_id, k.earlier_claim_id) for k in ks}) == len(ks)          # every pair once
    brain.pub(Control(flag="screen", value="final"))
    for _ in range(20):
        await brain.step(0.1)
        if brain.of("DebateSummary"):
            break
    s = brain.of("DebateSummary")[-1]
    assert s.concessions["prop"] == 0 and s.concessions["opp"] >= 1 and s.agreed


# ----------------------------------------------------------------------------- the mock + the profile
def test_mock_brain_simulates_the_planted_cases():
    from app.mocks.mock_brain import simulate_consistency, simulate_summary
    side = {"A": "prop", "B": "opp"}
    claims, found = [], []
    for spk, lines in fixture_turns():
        for line in lines:
            c = Claim(id=f"c{len(claims) + 1}", speaker=spk, side=side[spk], text=line, importance=.6)
            for f in simulate_consistency(c, claims, MOTION):
                found.append(Consistency(id=f"k{len(found)}", kind=f["kind"], claim_id=c.id,
                                         earlier_claim_id=f["earlier"].id, side=c.side))
            claims.append(c)
    assert sorted((k.kind, k.side) for k in found) == [("agreement", "opp"), ("concession", "opp"), ("concession", "opp"),
                                                       ("self_contradiction", "opp"), ("self_contradiction", "prop")]
    s = simulate_summary(MOTION, {c.id: c for c in claims}, found, {})
    assert len(s.agreed) == 3 and s.disputed and s.self_contradictions == {"prop": 1, "opp": 1}


def test_test_haiku_profile_pins_haiku_but_never_overrides_you(monkeypatch):
    from app import main
    for k in main.PROFILE_ENV["test-haiku"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EVIDENCE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(config, "CONSISTENCY_VOICE", False)
    applied = main.apply_profile_env("test-haiku")
    assert os.environ["LLM_MODEL"] == os.environ["CONSISTENCY_MODEL"] == "claude-haiku-5-5"
    assert os.environ["EVIDENCE_MODEL"] == "claude-sonnet-5-5" and "EVIDENCE_MODEL" not in applied
    assert config.CONSISTENCY_VOICE is True and config.LLM_MODEL == "claude-haiku-5-5"
    assert main.PROFILES["test-haiku"] == dict(ears="mock", brain="real", evidence="real", face="real")
    assert Path(os.environ["FIXTURE"]).name == "consistency_debate.jsonl"


def test_other_profiles_bring_no_env():
    from app import main
    assert main.apply_profile_env("mock") == {}
