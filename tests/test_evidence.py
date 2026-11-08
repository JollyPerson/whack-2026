"""Evidence: guards, retrieval, the check flow, and what reaches the bus. No network, no keys."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.bus import Bus
from app.evidence import preresearch
from app.evidence import run as run_module
from app.evidence.checks import CONFIDENCE_CAP, Source, apply_guards, normalise, number_appears, numbers_in, quote_in_source
from app.evidence.index import EvidenceIndex, chunk_text
from app.evidence.run import Checker, EvidenceModule
from app.schemas import Claim, SessionStart

TEXT = "Bus use outside London fell by 27% between 2010 and 2020, according to the Department for Transport's annual statistics."
SRC = [Source("https://gov.uk/bus", "Bus statistics", TEXT)]
QUOTE = "fell by 27% between 2010 and 2020"


def raw(**kw):
    base = {"label": "supported", "source": 0, "quote": QUOTE, "directly_states": True, "confidence": .9,
            "claimed": None, "actual": None, "unit": "", "short_fact": "it fell 27%", "explanation": "x"}
    return {**base, **kw}


class FakeLLM:
    name = "fake"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    async def complete_json(self, system, user, schema, timeout, max_tokens=400):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeSearch:
    name = "fake"

    def __init__(self, pages=None):
        self.pages, self.calls = pages or [], 0

    async def search(self, query, kind="auto", results=10, max_chars=4000, timeout=10.0):
        self.calls += 1
        return self.pages


# --- quotes
@pytest.mark.parametrize("quote", [QUOTE, "FELL BY 27% BETWEEN  2010 AND 2020", "Department for Transport’s annual"])
def test_quote_found_despite_typography(quote):
    assert quote_in_source(quote, TEXT)


@pytest.mark.parametrize("quote", ["fell by 30% between 2010 and 2020", "", "fell", "by 27% between 2010 and 2021"])
def test_quote_that_is_not_verbatim_fails(quote):
    assert not quote_in_source(quote, TEXT)


def test_fabricated_quote_drops_the_verdict():
    g = apply_guards(raw(quote="bus use collapsed by half"), "Bus use fell", SRC)
    assert g.label == "unverifiable" and not g.evidence and g.confidence == 0 and g.quote_rejected


@pytest.mark.parametrize("source", [5, -1, None, True, "0"])
def test_bad_source_number_drops_the_verdict(source):
    assert apply_guards(raw(source=source), "Bus use fell", SRC).label == "unverifiable"


def test_unverifiable_and_unknown_labels_carry_no_evidence():
    for label in ("unverifiable", "banana", None):
        g = apply_guards(raw(label=label), "x", SRC)
        assert g.label == "unverifiable" and g.evidence == []


# --- numbers
def test_numbers_parsed_with_commas_and_scale():
    assert numbers_in("about 1,200 people and 3.5 million cars, 27%") == [1200, 3.5e6, 27]
    assert number_appears(40000, "around 40,000 deaths") and not number_appears(40, "around 40,000 deaths")


def test_matching_number_stays_supported_at_full_confidence():
    g = apply_guards(raw(claimed=27, actual=27, unit="%"), "Bus use fell by 27%", SRC)
    assert g.label == "supported" and g.confidence == .9 and g.numeric.actual == 27


def test_code_overrules_model_when_numbers_differ():
    g = apply_guards(raw(claimed=50, actual=27), "Bus use fell by 50%", SRC)
    assert g.label == "contradicted" and "numeric_override" in g.notes and g.confidence == CONFIDENCE_CAP


def test_code_overrules_model_when_numbers_agree():
    g = apply_guards(raw(label="contradicted", claimed=27, actual=27), "Bus use fell by 27%", SRC)
    assert g.label == "supported" and g.confidence == CONFIDENCE_CAP


def test_numbers_within_five_percent_agree():
    assert apply_guards(raw(claimed=28, actual=27), "It fell by 28%", SRC).label == "supported"


def test_missing_context_is_not_overruled_by_numbers():
    assert apply_guards(raw(label="missing_context", claimed=27, actual=27), "It fell by 27%", SRC).label == "missing_context"


@pytest.mark.parametrize("claimed,actual", [(99, 27), (27, 99)])
def test_invented_numbers_are_discarded_and_capped(claimed, actual):
    g = apply_guards(raw(claimed=claimed, actual=actual), "It fell by 27%", SRC)
    assert g.numeric is None and "numeric_dropped" in g.notes and g.confidence == CONFIDENCE_CAP


# --- confidence
def test_quote_that_only_implies_the_fact_stays_off_air():
    assert apply_guards(raw(directly_states=False), "Bus use fell", SRC).confidence == CONFIDENCE_CAP


def test_claim_with_uncompared_figure_stays_off_air():
    assert apply_guards(raw(), "Bus use fell by 27%", SRC).confidence == CONFIDENCE_CAP


def test_direct_quote_keeps_its_confidence():
    assert apply_guards(raw(), "Bus use outside London fell", SRC).confidence == .9


def test_bad_confidence_values_are_safe():
    assert apply_guards(raw(confidence="high"), "Bus use fell", SRC).confidence == 0
    assert apply_guards(raw(confidence=85), "Bus use fell", SRC).confidence == 1.0


def test_short_fact_is_limited_to_ten_words():
    assert len(apply_guards(raw(short_fact=" ".join("abcdefghijklmnop")), "x", SRC).short_fact.split()) == 10


# --- index
def chunks():
    return [{"url": f"https://x.org/{i}", "title": str(i), "text": t} for i, t in enumerate(
        ["Bus use outside London fell sharply.", "Cycling rose in Ghent after car limits.", "Rail fares increased again this year."])]


def test_chunking_covers_text_and_keeps_sentences_whole():
    text = " ".join(f"Sentence number {i} ends here." for i in range(200))
    pieces = chunk_text(text)
    assert all(len(p) <= 900 for p in pieces) and pieces[0].endswith(".")
    assert "Sentence number 199 ends here." in pieces[-1] and "Sentence number 100 " in " ".join(pieces)


def test_bm25_finds_the_relevant_chunk():
    assert EvidenceIndex(chunks()).search("bus use in London", 1)[0]["url"].endswith("/0")


def test_search_limits_chunks_per_page():
    many = [{"url": "https://same.org", "title": "t", "text": f"bus use figure {i}"} for i in range(5)]
    assert len(EvidenceIndex(many).search("bus use", 5)) == 2


def test_vectors_join_the_ranking():
    np = pytest.importorskip("numpy")
    index = EvidenceIndex(chunks(), np.array([[1., 0], [0, 1.], [.7, .7]], dtype="float32"))
    assert index.search("zzz unrelated", 1, query_vector=[0, 1])[0]["url"].endswith("/1")


def test_index_round_trip(tmp_path):
    np = pytest.importorskip("numpy")
    EvidenceIndex(chunks(), np.eye(3, dtype="float32"), "e").save(tmp_path / "m")
    loaded = EvidenceIndex.load(tmp_path / "m")
    assert len(loaded) == 3 and loaded.embedder_name == "e" and loaded.vectors.shape == (3, 3)


def test_index_path_is_stable_and_safe():
    a = preresearch.index_path("This house would ban cars!")
    assert a == preresearch.index_path("this house  would ban cars!") and "/" not in a.name and "!" not in a.name


# --- the check flow
async def test_index_answer_does_not_touch_the_web():
    web = FakeSearch([{"url": "u", "title": "t", "text": TEXT}])
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    out = await Checker(FakeLLM(raw()), web, index=index).check("Bus use outside London fell")
    assert out.via == "index" and out.guarded.label == "supported" and web.calls == 0


async def test_unsure_index_falls_back_to_web():
    web = FakeSearch([{"url": "https://ons.gov.uk/a", "title": "ONS", "text": TEXT}])
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": "Bus use is discussed here."}])
    out = await Checker(FakeLLM(raw(label="unverifiable"), raw()), web, index=index).check("Bus use outside London fell")
    assert out.via == "web" and out.guarded.label == "supported" and out.guarded.evidence[0].url == "https://ons.gov.uk/a"


async def test_web_off_stays_unverifiable():
    web = FakeSearch([{"url": "u", "title": "t", "text": TEXT}])
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": "Bus use is discussed here."}])
    out = await Checker(FakeLLM(raw(label="unverifiable")), web, index=index).check("Bus use", web=False)
    assert out.guarded.label == "unverifiable" and web.calls == 0


async def test_web_failure_keeps_the_index_answer():
    class Down(FakeSearch):
        async def search(self, *a, **k):
            raise RuntimeError("down")
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": "Bus use is discussed here."}])
    out = await Checker(FakeLLM(raw(label="unverifiable")), Down(), index=index).check("Bus use")
    assert out.via == "index" and out.guarded.label == "unverifiable"


async def test_no_model_is_unverifiable():
    assert (await Checker(None).check("anything")).guarded.label == "unverifiable"


# --- pre-research
async def test_build_index_dedupes_pages_and_reports_progress():
    stages = []
    index = await preresearch.build_index("Motion", FakeSearch([{"url": "https://a.org", "title": "A", "text": TEXT}]),
                                          progress=lambda s, d, t, x: stages.append(s))
    assert len(index) == 1 and stages[0] == "searching" and stages[-1] == "ready" and "embedding" in stages


async def test_build_index_survives_failed_searches():
    class Down(FakeSearch):
        async def search(self, *a, **k):
            raise RuntimeError("down")
    assert len(await preresearch.build_index("Motion", Down())) == 0


async def test_query_planning_falls_back_to_templates():
    assert await preresearch.plan_queries("M", FakeLLM(RuntimeError("no"))) == preresearch.template_queries("M")


# --- on the bus
async def wait_for(sub, event, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ev = await asyncio.wait_for(sub.__anext__(), timeout)
        if ev.event == event:
            return ev
    raise AssertionError(event)


def claim(**kw):
    return Claim(**{"id": "c1", "speaker": "A", "text": "Bus use outside London fell", **kw})


async def test_module_publishes_source_card_and_traced_verdict():
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict", "SourceCard"})
    mod = EvidenceModule(bus, FakeLLM(raw()))
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    t_end = time.time() - .5
    await mod.handle_claim(claim(t_end=t_end))
    card, verdict = await sub.__anext__(), await sub.__anext__()
    assert card.event == "SourceCard" and card.snippet == QUOTE
    assert verdict.label == "supported" and verdict.via == "index" and verdict.latency_ms >= 400
    assert "retrieved" in verdict.marks and verdict.short_fact == "it fell 27%"


async def test_module_gives_every_failure_a_verdict_so_no_chip_hangs():
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})
    mod = EvidenceModule(bus, FakeLLM(RuntimeError("boom")))
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim())
    v = await sub.__anext__()
    assert v.label == "unverifiable" and v.via == "error"


async def test_stale_claim_is_not_checked_but_fixture_times_are_not_stale():
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})
    llm = FakeLLM(raw())
    mod = EvidenceModule(bus, llm)
    await mod.handle_claim(claim(t_end=time.time() - 100))
    assert (await sub.__anext__()).via == "stale" and llm.calls == 0
    await mod.handle_claim(claim(id="c2", t_end=10.5))
    assert (await sub.__anext__()).via != "stale"


async def test_verdict_from_an_old_session_is_dropped():
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})

    class NewSessionMidCheck(FakeLLM):
        async def complete_json(self, *a, **k):
            mod.session += 1
            return await super().complete_json(*a, **k)
    mod = EvidenceModule(bus, NewSessionMidCheck(raw()))
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim())
    assert sub.q.empty()


async def test_saved_index_is_used_without_searching(tmp_path, monkeypatch):
    monkeypatch.setattr(preresearch, "INDEX_DIR", tmp_path)
    EvidenceIndex(chunks()).save(preresearch.index_path("Motion"))
    bus = Bus()
    sub = bus.subscribe("t", {"ResearchProgress"})
    web = FakeSearch()
    mod = EvidenceModule(bus, FakeLLM(raw()), web)
    await mod.start_session("Motion")
    progress = await sub.__anext__()
    assert progress.stage == "ready" and progress.detail == "built in advance" and web.calls == 0 and len(mod.checker.index) == 3


async def test_motion_with_no_index_and_no_search_reports_it():
    bus = Bus()
    sub = bus.subscribe("t", {"ResearchProgress"})
    await EvidenceModule(bus, FakeLLM(raw())).start_session("A Motion nobody prepared")
    assert (await sub.__anext__()).stage == "error"


async def test_run_loop_only_checks_checkable_claims():
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})
    task = asyncio.create_task(EvidenceModule(bus, FakeLLM(raw())).run())
    await asyncio.sleep(0)
    bus.publish(SessionStart(motion=""))
    bus.publish(claim(id="no", checkable=False))
    bus.publish(claim(id="yes"))
    verdict = await asyncio.wait_for(sub.__anext__(), 2)
    task.cancel()
    assert verdict.claim_id == "yes"


def test_normalise_only_forgives_typography():
    assert normalise("A–B  “C”") == 'a-b "c"'


def test_demo_motions_are_five_and_have_distinct_indexes():
    motions = preresearch.demo_motions()
    assert len(motions) == 5 and len({preresearch.index_path(m) for m in motions}) == 5


def test_motions_file_has_four_eval_and_one_holdout():
    data = json.loads(preresearch.MOTIONS_FILE.read_text())["motions"]
    assert sorted(m["use"] for m in data) == ["eval"] * 4 + ["holdout"]


async def test_slow_query_embedding_falls_back_to_bm25(monkeypatch):
    monkeypatch.setattr(run_module, "QUERY_EMBED_TIMEOUT_S", .01)

    class Slow:
        name = "slow"

        def embed(self, texts):
            time.sleep(.2)
            return [[1.0, 0.0, 0.0]]
    np = pytest.importorskip("numpy")
    index = EvidenceIndex(chunks(), np.eye(3, dtype="float32"))
    found = await Checker(None, embedder=Slow(), index=index).retrieve("bus use in London")
    assert found[0].url.endswith("/0")


# --- on-the-fly pre-research: brief, ResearchRequest, fact sheet
PAGE = {"url": "https://gov.uk/bus", "title": "Bus statistics", "text": TEXT}
FACT = {"statement": "Bus use outside London fell 27% from 2010 to 2020.", "side": "opp", "source": 0,
        "quote": QUOTE, "number": 27, "unit": "%"}


def test_brief_changes_the_cache_key_but_no_brief_keeps_the_old_one():
    m = "This house would ban cars"
    assert preresearch.index_path(m) == preresearch.index_path(m, brief="  ")
    assert preresearch.index_path(m, brief="UK focus") != preresearch.index_path(m)
    assert preresearch.index_path(m, brief="UK  focus") == preresearch.index_path(m, brief="UK focus")


async def test_brief_reaches_the_query_planner():
    class Spy(FakeLLM):
        async def complete_json(self, system, user, *a, **k):
            self.user = user
            return {"queries": ["q1"]}
    llm = Spy()
    assert await preresearch.plan_queries("M", llm, brief="Mention Ghent") == ["q1"]
    assert "Mention Ghent" in llm.user and "Brief" in llm.user


async def test_fact_sheet_keeps_only_verbatim_quotes():
    index = EvidenceIndex([PAGE])
    made_up = {**FACT, "quote": "fell by 40% between 2010 and 2020"}
    bad_source = {**FACT, "source": 7}
    facts = await preresearch.extract_facts("M", index, FakeLLM({"facts": [FACT, made_up, bad_source, FACT]}),
                                            queries=["bus use"])
    assert len(facts) == 1
    f = facts[0]
    assert f["side"] == "opp" and f["url"] == PAGE["url"] and f["context"] == TEXT and f["number"] == 27


async def test_fact_sheet_survives_a_failing_model():
    assert await preresearch.extract_facts("M", EvidenceIndex([PAGE]), FakeLLM(RuntimeError("x")), queries=["q"]) == []


async def test_matching_fact_is_put_first_for_the_judge():
    other = [{"url": "https://x.org/a", "title": "A", "text": "Bus use outside London fell sharply."}]
    c = Checker(None, index=EvidenceIndex(other), facts=[{**FACT, "url": PAGE["url"], "title": "Bus", "context": TEXT}])
    found = await c.retrieve("bus use outside London fell 27%")
    assert found[0].url == PAGE["url"] and found[0].text == TEXT and len(found) == 2


async def test_research_request_builds_saves_and_publishes_a_fact_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(preresearch, "INDEX_DIR", tmp_path)
    bus = Bus()
    sub = bus.subscribe("t", {"FactSheet", "ResearchProgress", "SessionStart"})
    mod = EvidenceModule(bus, FakeLLM({"queries": ["bus"]}, {"facts": [FACT]}), FakeSearch([PAGE]))
    await mod.prepare("New motion", "UK only")
    sheet = await wait_for(sub, "FactSheet")
    assert sheet.motion == "New motion" and sheet.brief == "UK only" and sheet.facts[0].quote == QUOTE
    assert not hasattr(sheet.facts[0], "context") or "context" not in sheet.facts[0].model_dump()
    assert mod.session == 0                          # preparing never resets the running debate
    path = preresearch.index_path("New motion", brief="UK only")
    assert path.with_suffix(".json").exists() and preresearch.load_facts(path)[0]["context"] == TEXT


async def test_progress_says_ready_only_after_the_fact_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(preresearch, "INDEX_DIR", tmp_path)
    bus = Bus()
    sub = bus.subscribe("t", {"FactSheet", "ResearchProgress"})
    await EvidenceModule(bus, FakeLLM({"queries": ["bus"]}, {"facts": [FACT]}), FakeSearch([PAGE])).prepare("M2")
    seen = []
    while not sub.q.empty():
        ev = sub.q.get_nowait()
        seen.append(ev.stage if ev.event == "ResearchProgress" else ev.event)
    assert seen.index("facts") < seen.index("ready") and seen.count("ready") == 1


async def test_session_start_joins_a_build_already_in_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(preresearch, "INDEX_DIR", tmp_path)
    search = FakeSearch([PAGE])
    mod = EvidenceModule(Bus(), FakeLLM({"queries": ["bus"]}, {"facts": [FACT]}), search)
    prep = asyncio.create_task(mod.prepare("Joined", "brief"))
    await asyncio.sleep(0)
    await mod.start_session("Joined", "brief")
    await prep
    assert search.calls == 1 and len(mod.checker.index) == 1 and mod.checker.facts[0]["quote"] == QUOTE


async def test_saved_fact_sheet_loads_with_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(preresearch, "INDEX_DIR", tmp_path)
    path = preresearch.index_path("Saved")
    EvidenceIndex([PAGE]).save(path)
    preresearch.save_facts(path, [{**FACT, "url": PAGE["url"], "title": "Bus", "context": TEXT}])
    bus = Bus()
    sub = bus.subscribe("t", {"FactSheet"})
    mod = EvidenceModule(bus, FakeLLM(raw()), FakeSearch())
    await mod.start_session("Saved", "a brief nobody researched")   # falls back to the demo Motion's index
    assert len(mod.checker.facts) == 1 and (await sub.__anext__()).facts[0].statement == FACT["statement"]


async def test_research_request_without_search_reports_it():
    bus = Bus()
    sub = bus.subscribe("t", {"ResearchProgress"})
    await EvidenceModule(bus, FakeLLM(raw())).prepare("Unprepared motion")
    p = await sub.__anext__()
    assert p.stage == "error" and p.motion == "Unprepared motion"


async def test_mock_evidence_answers_a_research_request():
    from app.mocks import mock_evidence
    bus = Bus()
    sub = bus.subscribe("t", {"FactSheet"})
    await mock_evidence.fake_research(bus, "This house would test", "b", step=0)
    sheet = await sub.__anext__()
    assert sheet.motion == "This house would test" and {f.side for f in sheet.facts} == {"prop", "opp", "both"}
