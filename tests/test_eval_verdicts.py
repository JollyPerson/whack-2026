"""The verdict eval's scoring and gates. No network, no keys."""
from __future__ import annotations

import json

from app.evidence.checks import Guarded, Source
from app.schemas import Evidence
from eval import eval_verdicts as ev

ROW = {"gid": "g", "side": "prop", "label": "supported", "text": "t", "motion": "m", "sources": ["https://a.org/x/"], "key": "4.2%"}
SRC = Source("https://a.org/x", "A", "Output grew by 4.2% last year in the region.")


def result(gid="g", gold="supported", label="supported", conf=.5, fabricated=False, ms=100.0, via="saved", side="prop"):
    return ev.Result(gid, side, gold, label, conf, fabricated, False, ms, via)


def test_balanced_set_has_no_problems():
    rows = []
    n = 0
    for label in ev.LABELS:
        for j in range(5):
            rows.append({"gid": str(n), "motion": f"m{n % 4}", "side": "prop" if n % 2 else "opp", "label": label, "sources": ["u"], "key": "k"})
            n += 1
    assert ev.balance_problems(rows) == []


def test_lopsided_set_is_reported():
    problems = ev.balance_problems([{"gid": "a", "motion": "m", "side": "prop", "label": "supported"}])
    assert any("1 claims" in p for p in problems) and any("needs accepted sources" in p for p in problems)


def test_holdout_rows_are_kept_apart(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({"gid": "a"}) + "\n" + json.dumps({"gid": "b", "set": "holdout"}) + "\n")
    assert [r["gid"] for r in ev.load_gold(p)] == ["a"] and [r["gid"] for r in ev.load_gold(p, True)] == ["b"]


def test_hit_by_accepted_url_or_key_phrase():
    assert ev.is_hit(ROW, [Source("https://a.org/x", "", "nothing")])
    assert ev.is_hit(ROW, [Source("https://other.org", "", "It grew by 4.2% overall")])
    assert not ev.is_hit(ROW, [{"url": "https://other.org", "text": "no figures"}])


def test_audit_catches_a_quote_the_source_does_not_contain():
    bad = Guarded(label="supported", confidence=.9, evidence=[Evidence(url=SRC.url, title="A", quote="Output collapsed by half last year")])
    good = Guarded(label="supported", confidence=.9, evidence=[Evidence(url=SRC.url, title="A", quote="Output grew by 4.2% last year")])
    assert ev.audit(ROW, bad, [SRC], 1, "saved").fabricated
    assert not ev.audit(ROW, good, [SRC], 1, "saved").fabricated


def test_audit_catches_a_real_quote_credited_to_the_wrong_page():
    wrong = Guarded(label="supported", evidence=[Evidence(url="https://elsewhere.org", title="E", quote="Output grew by 4.2% last year")])
    assert ev.audit(ROW, wrong, [SRC], 1, "saved").fabricated


def passing(n=20):
    return [result(gid=str(i)) for i in range(n)]


def test_clean_run_passes():
    assert ev.gate_failures(ev.score(passing())) == []


def test_fourteen_of_twenty_is_the_line():
    def run(wrong):
        return [result(gid=str(i), label="unverifiable" if i < wrong else "supported") for i in range(20)]
    assert ev.gate_failures(ev.score(run(6))) == []
    assert any("13 of 20" in f for f in ev.gate_failures(ev.score(run(7))))


def test_fabricated_quote_fails_however_good_the_score():
    assert any("fabricated" in f for f in ev.gate_failures(ev.score(passing(19) + [result(fabricated=True)])))


def test_swap_fails_only_at_speaking_confidence():
    quiet = passing(19) + [result(gid="s", gold="supported", label="contradicted", conf=.74)]
    loud = passing(19) + [result(gid="s", gold="contradicted", label="supported", conf=.75)]
    assert ev.gate_failures(ev.score(quiet)) == []
    assert any("swaps" in f and "s" in f for f in ev.gate_failures(ev.score(loud)))


def test_other_errors_at_high_confidence_are_not_swaps():
    m = ev.score([result(gold="supported", label="missing_context", conf=.9)])
    assert m["swaps_at_speaking_confidence"] == []


def test_latency_gate_uses_index_answers_only():
    slow_web = passing(19) + [result(ms=9000, via="web")]
    slow_index = [result(gid=str(i), ms=3000) for i in range(20)]
    assert ev.gate_failures(ev.score(slow_web)) == []
    assert any("p95" in f for f in ev.gate_failures(ev.score(slow_index)))


def test_holdout_needs_three_of_five():
    def run(right):
        return [result(gid=str(i), label="supported" if i < right else "unverifiable") for i in range(5)]
    assert ev.gate_failures(ev.score(run(3)), holdout=True) == []
    assert ev.gate_failures(ev.score(run(2)), holdout=True)


def test_report_shows_the_counts_and_the_sides():
    text = ev.report(ev.score([result(), result(gid="b", side="opp", label="unverifiable")]))
    assert "correct 1/2" in text and "prop 1/1" in text and "opp 0/1" in text


def test_example_file_matches_the_format():
    rows = ev.load_gold(ev.ROOT / "eval" / "verdict_gold.example.jsonl")
    assert rows and all(r["gid"].startswith("example") and r["label"] in ev.LABELS for r in rows)


# --- regressions
GOLD_UNVERIFIABLE = {**ROW, "label": "unverifiable"}


class Raises:
    name = "raises"

    def __init__(self, error):
        self.error = error

    async def complete_json(self, *a, **k):
        raise self.error


async def test_a_model_error_is_counted_and_never_correct():
    results = await ev.run_verdict_once([GOLD_UNVERIFIABLE], Raises(TimeoutError()), {"g": [SRC]})
    m = ev.score(results)
    assert m["correct"] == 0 and m["errors"] == 1 and "errors 1" in ev.report(m)


async def test_full_layer_error_is_counted(monkeypatch):
    from app.evidence.index import EvidenceIndex
    monkeypatch.setattr(ev, "_load_index", lambda motion, cache: EvidenceIndex([{"url": SRC.url, "title": "A", "text": SRC.text}]))
    row = {**GOLD_UNVERIFIABLE, "text": "Output grew last year"}
    results = await ev.run_full_once([row], Raises(RuntimeError("bad key")), None, None, {})
    m = ev.score(results)
    assert results[0].via == "error" and m["correct"] == 0 and m["errors"] == 1


def test_results_without_errors_score_as_before():
    assert ev.score(passing())["errors"] == 0


async def test_save_evidence_keeps_the_old_file_when_retrieval_fails(tmp_path, monkeypatch):
    saved = tmp_path / "saved.jsonl"
    saved.write_text('{"gid": "hand", "sources": []}\n')
    calls = []

    async def retrieve(row, cache, embedder):
        calls.append(row["gid"])
        if len(calls) == 2:
            raise SystemExit("No saved index")
        return [SRC]
    monkeypatch.setattr(ev, "SAVED", saved)
    monkeypatch.setattr(ev, "_retrieve", retrieve)
    monkeypatch.setattr(ev, "make_embedder", lambda: None)
    import pytest
    with pytest.raises(SystemExit):
        await ev.save_evidence([ROW, {**ROW, "gid": "h"}])
    assert saved.read_text() == '{"gid": "hand", "sources": []}\n' and [p.name for p in tmp_path.iterdir()] == ["saved.jsonl"]


async def test_save_evidence_writes_every_row(tmp_path, monkeypatch):
    saved = tmp_path / "saved.jsonl"

    async def retrieve(row, cache, embedder):
        return [SRC]
    monkeypatch.setattr(ev, "SAVED", saved)
    monkeypatch.setattr(ev, "_retrieve", retrieve)
    monkeypatch.setattr(ev, "make_embedder", lambda: None)
    assert await ev.save_evidence([ROW, {**ROW, "gid": "h"}]) == 0
    assert [json.loads(line)["gid"] for line in saved.read_text().splitlines()] == ["g", "h"]
