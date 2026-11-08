"""Evidence: guards, retrieval, the check flow, and what reaches the bus. No network, no keys."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.bus import Bus
from app import config
from app.evidence import cache as cache_module
from app.evidence import deep, preresearch
from app.evidence.cache import VerdictCache
from app.evidence import run as run_module
from app.evidence.checks import CONFIDENCE_CAP, Source, apply_guards, normalise, number_appears, numbers_in, quote_in_source
from app.evidence.index import EvidenceIndex, chunk_text, tokens
from app.evidence.run import Checker, EvidenceModule
from app.schemas import Claim, SessionStart

TEXT = "Bus use outside London fell by 27% between 2010 and 2020, according to the Department for Transport's annual statistics."
SRC = [Source("https://gov.uk/bus", "Bus statistics", TEXT)]
QUOTE = "fell by 27% between 2010 and 2020"


@pytest.fixture(autouse=True)
def no_deep_check(monkeypatch):
    """The deep check runs in the background after each verdict; tests that want it turn it back on."""
    monkeypatch.setitem(config.FLAGS, "deep_check", False)


@pytest.fixture(autouse=True)
def verdict_cache_in_tmp(tmp_path, monkeypatch):
    """Never read or write the real cache/verdicts folder."""
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path / "verdicts")


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

    async def search(self, query, kind="auto", results=10, max_chars=4000, timeout=10.0, include_domains=None):
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


def test_written_figures_match_how_they_are_said():
    assert tokens("Evasion was 12.52% (£617m), up 5 per cent to £3.8bn") == [
        "evasion", "12.52", "percent", "pounds", "617", "million", "up", "5", "percent", "pounds", "3.8", "billion"]
    assert {"12", "percent", "600", "million", "pounds"} <= set(tokens("12 percent, 600 million pounds"))


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


class SplitSearch(FakeSearch):
    """One page per search, titled by whether the search was limited to primary sources."""
    async def search(self, query, include_domains=None, max_chars=4000, **k):
        self.calls += 1
        self.max_chars = max_chars
        kind = "primary" if include_domains == preresearch.PRIMARY_DOMAINS else "open"
        return [{"url": f"https://{kind}.org/{query}", "title": kind, "text": TEXT}]


class FakeEmbedder:
    name = "fake"

    def embed(self, texts):
        return [[1.0, 0.0]] * len(texts)


async def test_build_with_vectors_adds_a_primary_pass_and_keeps_its_pages_first(monkeypatch):
    monkeypatch.delenv("PRERESEARCH_PRIMARY", raising=False)
    searcher = SplitSearch()
    index = await preresearch.build_index("Motion", searcher, embedder=FakeEmbedder())
    queries = preresearch.template_queries("Motion")
    assert searcher.calls == 2 * len(queries) and searcher.max_chars == preresearch.WITH_VECTORS.max_chars
    assert [c["title"] for c in index.chunks] == ["primary"] * len(queries) + ["open"] * len(queries)
    assert index.vectors is not None


async def test_retrieval_shows_more_chunks_when_vectors_rank_them():
    import numpy as np
    many = [{"url": f"https://x.org/{i}", "title": str(i), "text": f"Bus use fell {i}"} for i in range(20)]
    with_vectors = EvidenceIndex(many, np.ones((20, 2), dtype="float32") / np.sqrt(2), "fake")
    assert len(await Checker(None, embedder=FakeEmbedder(), index=with_vectors).retrieve("Bus use")) == 8
    assert len(await Checker(None, index=EvidenceIndex(many)).retrieve("Bus use")) == 5


async def test_build_without_vectors_stays_small(monkeypatch):
    monkeypatch.delenv("PRERESEARCH_PRIMARY", raising=False)
    searcher = SplitSearch()
    index = await preresearch.build_index("Motion", searcher)
    assert searcher.calls == len(preresearch.template_queries("Motion")) and searcher.max_chars == 4000
    assert {c["title"] for c in index.chunks} == {"open"}


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


async def test_live_build_skips_embedding_so_the_server_cpu_stays_free(tmp_path, monkeypatch):
    monkeypatch.setattr(preresearch, "INDEX_DIR", tmp_path)
    mod = EvidenceModule(Bus(), FakeLLM(RuntimeError("no plan")), FakeSearch([{"url": "https://a.org", "title": "A", "text": TEXT}]),
                         embedder=FakeEmbedder())
    await mod.start_session("A Motion nobody prepared")
    assert len(mod.checker.index) and mod.checker.index.vectors is None


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


# --- deep check
def guarded(label="supported", confidence=0.9):
    from app.evidence.checks import Guarded
    return Guarded(label=label, confidence=confidence)


@pytest.mark.parametrize("fast,deep_verdict,replaced", [
    (("unverifiable", 0), ("supported", .8), True),        # found what the fast check couldn't
    (("supported", .9), ("unverifiable", 0), False),       # not finding it again doesn't undo a verified quote
    (("unverifiable", 0), ("unverifiable", 0), False),
    (("supported", .6), ("supported", .8), True),          # surer, so it may now be said aloud
    (("supported", .8), ("supported", .8), False),
    (("contradicted", .7), ("missing_context", .8), True), # disagrees and is at least as sure
    (("contradicted", .9), ("supported", .7), False),      # disagrees but less sure
])
def test_deep_verdict_only_replaces_the_fast_one_when_it_does_better(fast, deep_verdict, replaced):
    assert deep.replaces(guarded(*fast), guarded(*deep_verdict)) is replaced


class RecordingSearch(FakeSearch):
    def __init__(self, pages=None):
        super().__init__(pages)
        self.queries = []

    async def search(self, query, include_domains=None, **k):
        self.queries.append((query, include_domains is not None))
        return await super().search(query)


async def test_deep_check_searches_written_queries_in_primary_sources_and_the_open_web():
    plan = {"queries": ["DfT bus statistics 2020 local bus passenger journeys"]}
    searcher = RecordingSearch([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    checking = deep.DeepChecker(FakeLLM(raw()), searcher, "Motion", plan_llm=FakeLLM(plan))
    got, sources = await checking.check("Bus use outside London fell", [])
    assert got.label == "supported" and sources[0].url == "https://gov.uk/bus"
    assert sorted(searcher.queries) == sorted([(q, p) for q in ("Bus use outside London fell", plan["queries"][0])
                                               for p in (True, False)])


async def test_deep_check_survives_failed_planning_and_search():
    class Down(FakeSearch):
        async def search(self, *a, **k):
            raise RuntimeError("down")
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    checking = deep.DeepChecker(FakeLLM(raw()), Down(), "Motion", index, plan_llm=FakeLLM(RuntimeError("no")))
    got, sources = await checking.check("Bus use outside London fell", [])
    assert got.label == "supported" and [s.url for s in sources] == ["https://gov.uk/bus"]


def test_written_queries_rank_a_passage_the_spoken_claim_misses():
    pages = [{"url": "https://other.org", "title": "x", "text": "Households said things about licences and pounds."},
             {"url": "https://gov.uk/trust", "title": "Trust Statement", "text": "The estimated evasion rate was 12.52%."}]
    queries = ["TV licence evasion rate", "Licence Fee Trust Statement estimated evasion rate"]
    top = deep.DeepChecker.rank("Over twelve in a hundred households dodge it", queries, pages, [])
    assert top[0].url == "https://gov.uk/trust"


async def test_module_publishes_a_deep_verdict_after_an_unverifiable_fast_one(monkeypatch):
    monkeypatch.setitem(config.FLAGS, "deep_check", True)
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})
    unsure = raw(label="unverifiable")
    llm = FakeLLM(unsure, unsure, {"queries": []}, raw())   # index verdict, web verdict, deep plan, deep verdict
    mod = EvidenceModule(bus, llm, RecordingSearch([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}]))
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim())
    first = await sub.__anext__()
    second = await asyncio.wait_for(sub.__anext__(), 2)
    assert (first.label, first.via) == ("unverifiable", "web")
    assert (second.label, second.via) == ("supported", "deep") and second.evidence


async def test_module_skips_the_deep_check_for_minor_claims(monkeypatch):
    monkeypatch.setitem(config.FLAGS, "deep_check", True)
    bus = Bus()
    llm = FakeLLM(raw(label="unverifiable"))
    mod = EvidenceModule(bus, llm)
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim(importance=0.2))
    await asyncio.sleep(0.05)
    assert llm.calls == 1 and not mod.tasks


async def test_deep_verdict_from_an_old_session_is_dropped(monkeypatch):
    monkeypatch.setitem(config.FLAGS, "deep_check", True)
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})

    class NewSessionDuringDeepCheck(FakeLLM):
        async def complete_json(self, *a, **k):
            if self.calls == 1:                 # the fast verdict is done: now the Motion changes
                mod.session += 1
            return await super().complete_json(*a, **k)
    mod = EvidenceModule(bus, NewSessionDuringDeepCheck(raw(label="unverifiable"), {"queries": []}, raw()))
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim())
    await sub.__anext__()
    await asyncio.gather(*mod.tasks)
    assert sub.q.empty()


# --- verdict cache
def settled(label="supported", confidence=0.9, **kw):
    return apply_guards(raw(label=label, confidence=confidence, **kw), "Bus use outside London fell by 27%", SRC)


CLAIM = "Bus use outside London fell by 27% between 2010 and 2020"


def test_cache_matches_a_rephrasing_with_the_same_figures_only():
    cache = VerdictCache()
    assert cache.add(CLAIM, settled(), "index")
    assert cache.lookup("bus use outside of London fell by 27% between 2010 and 2020").guarded.label == "supported"
    assert cache.lookup("Bus use outside London fell by 30% between 2010 and 2020") is None    # other figure
    assert cache.lookup("Bus use outside London did not fall by 27% between 2010 and 2020") is None
    assert cache.lookup("Rail fares rose by 27% between 2010 and 2020") is None


@pytest.mark.parametrize("g", [settled(label="unverifiable"), settled(confidence=0.3), settled(quote="not in the source")])
def test_cache_only_keeps_verdicts_worth_repeating(g):
    cache = VerdictCache()
    assert not cache.add(CLAIM, g, "index") and len(cache) == 0


def test_cache_persists_per_motion_and_newer_verdicts_win():
    first = VerdictCache.for_motion("Motion")
    first.add(CLAIM, settled(confidence=0.7), "index")
    first.add(CLAIM, settled(label="contradicted", confidence=0.8), "deep")
    reloaded = VerdictCache.for_motion("Motion")
    assert len(reloaded) == 1 and reloaded.lookup(CLAIM).guarded.label == "contradicted"
    assert reloaded.lookup(CLAIM).guarded.evidence[0].quote == QUOTE
    assert VerdictCache.for_motion("Another motion").lookup(CLAIM) is None


def test_cache_skips_a_bad_line():
    VerdictCache.for_motion("Motion").add(CLAIM, settled(), "index")
    path = preresearch.index_path("Motion", cache_module.CACHE_DIR).with_suffix(".jsonl")
    path.write_text("not json\n" + path.read_text())
    assert VerdictCache.for_motion("Motion").lookup(CLAIM) is not None


async def test_module_answers_a_repeated_claim_from_the_cache_without_checking():
    bus = Bus()
    sub = bus.subscribe("t", {"Verdict"})
    llm = FakeLLM(raw())
    mod = EvidenceModule(bus, llm)
    await mod.start_session("Motion")
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim(text=CLAIM))
    await mod.handle_claim(claim(id="c2", text="bus use outside London fell by 27% between 2010 and 2020"))
    first, second = await sub.__anext__(), await sub.__anext__()
    assert (first.via, second.via) == ("index", "cache") and second.label == "supported" and second.evidence
    assert llm.calls == 1
    again = EvidenceModule(Bus(), FakeLLM(raw()))
    await again.start_session("Motion")
    assert again.verdicts.lookup(CLAIM) is not None      # a later debate on the Motion


async def test_module_checks_again_when_the_cache_is_off(monkeypatch):
    monkeypatch.setitem(config.FLAGS, "verdict_cache", False)
    llm = FakeLLM(raw())
    mod = EvidenceModule(Bus(), llm)
    mod.checker.index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    await mod.handle_claim(claim(text=CLAIM))
    await mod.handle_claim(claim(id="c2", text=CLAIM))
    assert llm.calls == 2


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
