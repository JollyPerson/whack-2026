"""Evidence: guards, retrieval, the check flow, and what reaches the bus. No network, no keys."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.bus import Bus
from app.evidence import preresearch
from app.evidence import run as run_module
from app.evidence.checks import (CONFIDENCE_CAP, Source, apply_guards, normalise, number_appears, numbers_in,
                                 quote_in_source)
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


# --- figures said in words (most of docs/FACT-CARDS.md's WRONG facts)
GHG = "Domestic transport was the largest emitting sector in 2023, producing 29% of UK greenhouse gas emissions."
GHG_SRC, GHG_QUOTE = [Source("https://gov.uk/ghg", "GHG statistics", GHG)], "producing 29% of UK greenhouse gas emissions"
HALF = "Transport produces half of the UK's greenhouse gas emissions."


@pytest.mark.parametrize("text,value", [
    ("about a quarter of a million vacancies", 250_000), ("more than half a million people were hurt", 500_000),
    ("more than two million over-75s", 2e6), ("one and a half million", 1.5e6), ("half of the UK's emissions", 50),
    ("Three-quarters of the world's countries", 75), ("three quarters of countries", .75), ("One in five people", 20),
    ("1 in 20 people", .05), ("fallen by a third since 2010", 33), ("fallen by a third", 33.3), ("two-thirds of firms", 66.7),
])
def test_figures_said_in_words_appear(text, value):
    assert number_appears(value, text)


@pytest.mark.parametrize("text,value", [("a quarter of a million", 25), ("a third", 25), ("half a million", 5e6),
                                        ("one in five", 5), ("the third runway", 3)])
def test_figures_said_in_words_are_not_any_number(text, value):
    assert not number_appears(value, text)


def test_wrong_figure_in_words_is_compared_and_stays_on_air():
    g = apply_guards(raw(label="contradicted", quote=GHG_QUOTE, claimed=50, actual=29, unit="%"), HALF, GHG_SRC)
    assert g.label == "contradicted" and g.confidence == .9 and g.numeric.claimed == 50 and g.notes == []


def test_fraction_written_as_a_proportion_is_compared_as_a_percentage():
    g = apply_guards(raw(label="contradicted", quote=GHG_QUOTE, claimed=.5, actual=29, unit="%"), HALF, GHG_SRC)
    assert g.numeric.claimed == 50 and g.label == "contradicted" and g.confidence == .9


def test_rounded_figure_in_words_is_not_overruled():
    """\"About a quarter\" for 27% is fair: words get a looser band than digits (5%), so code doesn't call it wrong."""
    g = apply_guards(raw(claimed=25, actual=27, unit="%"), "Bus use fell by about a quarter", SRC)
    assert g.label == "supported" and g.confidence == .9 and g.notes == []


def test_figure_in_words_left_out_by_the_model_is_not_capped():
    """The prompt lets the model skip figures said in words; that must not count as an uncompared figure."""
    g = apply_guards(raw(label="contradicted", quote=GHG_QUOTE), HALF, GHG_SRC)
    assert g.confidence == .9 and g.notes == []


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
    index = EvidenceIndex(chunks(), np.eye(3, dtype="float32"), "slow")
    found = await Checker(None, embedder=Slow(), index=index).retrieve("bus use in London")
    assert found[0].url.endswith("/0")


# --- regressions: guards
@pytest.mark.parametrize("kw,text", [(dict(claimed=50, actual=27), "Bus use fell by 50%"),
                                     (dict(directly_states=False), "Bus use fell"),
                                     (dict(claimed=99, actual=27), "Bus use fell by 27%"),
                                     ({}, "Bus use fell by 27%")])
def test_guard_capped_verdict_is_marked_even_below_the_cap(kw, text):
    """At aggressiveness 4-5 the Director voices .65+, so a cap the guard chose must be marked whatever the confidence."""
    g = apply_guards(raw(confidence=.7, **kw), text, SRC)
    assert "capped" in g.notes and g.confidence == .7


def test_figures_followed_by_letters_are_read_whole():
    assert numbers_in("20mph zones") == [20] and numbers_in("over-75s") == [75] and numbers_in("the 1990s") == [1990]
    assert numbers_in("5m people, 3 metres, 12km") == [5e6, 3, 12] and numbers_in("COVID-19 cases") == [19]


WALES = "The default speed limit on restricted roads in Wales changed from 30mph to 20mph in September 2023."
WALES_SRC = [Source("https://gov.wales/20mph", "20mph", WALES)]


def test_figure_with_a_unit_attached_is_compared():
    g = apply_guards(raw(quote="changed from 30mph to 20mph", claimed=20, actual=20, unit="mph"),
                     "The default limit in Wales is now 20mph", WALES_SRC)
    assert g.numeric is not None and g.numeric.actual == 20 and g.notes == [] and g.confidence == .9


RAIL = "Passenger fares brought in £11.2 billion in the year to March."
RAIL_SRC, RAIL_QUOTE = [Source("https://orr.gov.uk/rail", "Rail finance", RAIL)], "Passenger fares brought in £11.2 billion"


def test_figure_given_without_its_scale_word_is_compared():
    """FACT-CARDS WRONG #8: the model writes 2 and 11.2 with unit "billion pounds"; both mean billions."""
    g = apply_guards(raw(label="contradicted", quote=RAIL_QUOTE, claimed=2, actual=11.2, unit="billion pounds"),
                     "Rail fares only bring in about £2 billion a year", RAIL_SRC)
    assert g.label == "contradicted" and g.confidence == .9 and g.notes == []
    assert (g.numeric.claimed, g.numeric.actual) == (2, 11.2)


@pytest.mark.parametrize("text,quote,actual", [
    ("It cost £3.2 billion", "It cost 3,200,000,000 pounds", 3.2e9),     # a mantissa against a full figure
    ("It cost £3.2 billion", "It cost £3.2 million", 3.2),               # billions against millions
])
def test_mantissa_is_never_compared_with_another_basis(text, quote, actual):
    g = apply_guards(raw(label="contradicted", quote=quote, claimed=3.2, actual=actual), text, [Source("u", "t", quote)])
    assert g.numeric is None and "numeric_dropped" in g.notes and g.label == "contradicted"


def test_close_figure_in_words_does_not_flip_contradicted_to_supported():
    """The 25% band for words only stops code calling a fair "about a quarter" wrong; "half" is not 38%."""
    src = [Source("u", "t", "Transport produced 38% of emissions in 2023.")]
    g = apply_guards(raw(label="contradicted", quote="Transport produced 38% of emissions", claimed=50, actual=38, unit="%"),
                     "Transport produces half of emissions", src)
    assert g.label == "contradicted" and "numeric_override" not in g.notes and "capped" in g.notes


def test_far_figure_in_words_still_overrules_supported():
    g = apply_guards(raw(quote=GHG_QUOTE, claimed=50, actual=29, unit="%"), HALF, GHG_SRC)
    assert g.label == "contradicted" and "numeric_override" in g.notes and "capped" in g.notes


ROADS = "More than half a million people were hurt on Britain's roads in 2024."
ROADS_TEXT = "In 2024 there were an estimated 128,272 casualties of all severities on Britain's roads."
ROADS_SRC, ROADS_QUOTE = [Source("https://gov.uk/roads", "Road casualties", ROADS_TEXT)], "an estimated 128,272 casualties"


def test_prompt_asks_for_figures_said_in_words():
    """The code compares figures said in words, so the prompt must not tell the model to leave them out."""
    from app.evidence import verdicts
    assert "counts as no figure" not in verdicts.SYSTEM and "500000" in verdicts.SYSTEM


def test_word_figure_reported_beside_a_year_is_compared_and_stays_on_air():
    g = apply_guards(raw(label="contradicted", quote=ROADS_QUOTE, claimed=500000, actual=128272), ROADS, ROADS_SRC)
    assert g.label == "contradicted" and g.confidence == .9 and g.notes == []


# --- regressions: index
def test_query_vector_of_the_wrong_size_leaves_bm25_to_answer():
    np = pytest.importorskip("numpy")
    index = EvidenceIndex(chunks(), np.eye(3, dtype="float32"), "e")
    assert index.search("bus use in London", 1, query_vector=[1.0] * 5)[0]["url"].endswith("/0")


def test_save_without_vectors_removes_old_vectors(tmp_path):
    np = pytest.importorskip("numpy")
    five = [{"url": f"https://x.org/{i}", "title": "t", "text": f"bus use {i}"} for i in range(5)]
    EvidenceIndex(five, np.eye(5, dtype="float32"), "e").save(tmp_path / "m")
    EvidenceIndex(five[:1]).save(tmp_path / "m")
    idx = EvidenceIndex.load(tmp_path / "m")
    assert idx.vectors is None and len(idx.search("bus use", 5, query_vector=[1, 0, 0, 0, 0])) == 1


def test_load_drops_vectors_that_do_not_match_the_chunks(tmp_path):
    np = pytest.importorskip("numpy")
    EvidenceIndex(chunks(), np.eye(3, dtype="float32"), "e").save(tmp_path / "m")
    np.save(tmp_path / "m.npy", np.eye(5, dtype="float32"))
    idx = EvidenceIndex.load(tmp_path / "m")
    assert idx.vectors is None and idx.search("bus use", 3, query_vector=[0, 0, 0, 0, 1])


def test_save_leaves_no_temporary_files(tmp_path):
    np = pytest.importorskip("numpy")
    EvidenceIndex(chunks(), np.eye(3, dtype="float32"), "e").save(tmp_path / "m")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.json", "m.npy"]


def test_openai_embedder_reuses_one_connection(monkeypatch):
    import httpx
    from app.evidence import index as index_module
    made, sent = [], []

    def reply(request):
        sent.append(request)
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)]} for i in range(n)]})

    class Counting(httpx.Client):
        def __init__(self, **kw):
            made.append(self)
            super().__init__(transport=httpx.MockTransport(reply), **kw)
    monkeypatch.setattr(index_module.httpx, "Client", Counting)
    monkeypatch.setattr(index_module.httpx, "post", lambda *a, **k: pytest.fail("a new connection for every call"))
    embedder = index_module.OpenAIEmbedder("k")
    assert embedder.embed(["a"]) == [[0.0]] and embedder.embed(["a", "b"]) == [[0.0], [1.0]]
    assert len(made) == 1 and len(sent) == 2 and sent[0].headers["authorization"] == "Bearer k"


# --- regressions: the check flow
class NamedEmbedder:
    def __init__(self, name, vector):
        self.name, self.vector, self.calls = name, vector, 0

    def embed(self, texts):
        self.calls += 1
        return [self.vector for _ in texts]


async def test_index_built_by_another_embedder_answers_from_bm25():
    np = pytest.importorskip("numpy")
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}],
                          np.ones((1, 4), dtype="float32") / 2, "fastembed:BAAI/bge-small-en-v1.5")
    web, other = FakeSearch(), NamedEmbedder("openai:text-embedding-3-small", [1.0] * 6)
    out = await Checker(FakeLLM(raw()), web, embedder=other, index=index).check("Bus use outside London fell")
    assert out.via == "index" and out.guarded.label == "supported" and web.calls == 0 and other.calls == 0


async def test_matching_embedder_is_used_for_the_query():
    np = pytest.importorskip("numpy")
    index = EvidenceIndex(chunks(), np.eye(3, dtype="float32"), "same")
    same = NamedEmbedder("same", [0.0, 1.0, 0.0])
    found = await Checker(None, embedder=same, index=index).retrieve("zzz unrelated")
    assert same.calls == 1 and found[0].url.endswith("/1")


async def test_index_model_error_still_tries_the_web():
    from app.brain.llm import LLMInvalid
    web = FakeSearch([{"url": "https://ons.gov.uk/a", "title": "ONS", "text": TEXT}])
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    llm = FakeLLM(LLMInvalid("stop_reason=max_tokens"), raw())
    out = await Checker(llm, web, index=index).check("Bus use outside London fell")
    assert out.via == "web" and out.guarded.label == "supported" and web.calls == 1


@pytest.mark.parametrize("web_on", [True, False])
async def test_index_model_error_with_no_web_answer_is_still_an_error(web_on):
    from app.brain.llm import LLMRefusal
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    with pytest.raises(LLMRefusal):
        await Checker(FakeLLM(LLMRefusal("no")), FakeSearch([]), index=index).check("Bus use outside London fell", web=web_on)


async def test_index_model_timeout_does_not_spend_the_rest_on_the_web():
    from app.brain.llm import LLMTimeout
    web = FakeSearch([{"url": "https://ons.gov.uk/a", "title": "ONS", "text": TEXT}])
    index = EvidenceIndex([{"url": "https://gov.uk/bus", "title": "Bus", "text": TEXT}])
    with pytest.raises(LLMTimeout):
        await Checker(FakeLLM(LLMTimeout("slow"), raw()), web, index=index).check("Bus use outside London fell")
    assert web.calls == 0
