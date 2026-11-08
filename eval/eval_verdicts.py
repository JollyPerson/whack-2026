"""Score Evidence against the labelled claims. Owner: C. The design and gates are recorded on issue #15.

  python -m eval.eval_verdicts --check                        # is the gold set balanced? no keys needed
  python -m eval.eval_verdicts --save-evidence                # save each claim's top 5 chunks, then add any missing passage
  python -m eval.eval_verdicts --layer retrieval              # recall@5 on the saved indexes
  python -m eval.eval_verdicts --layer verdict --runs 3       # model comparison on saved evidence, web off
  python -m eval.eval_verdicts --layer verdict                # regression check
  python -m eval.eval_verdicts --layer full                   # headline: index, then web. Reported separately
  python -m eval.eval_verdicts --layer deep                   # full, then the background deep check on every claim
  python -m eval.eval_verdicts --layer verdict --holdout      # fifth Motion. Once, after tuning ends

Set EVIDENCE_INDEX_DIR to the saved copy of the indexes so a rebuild can't move the score.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402,F401  (loads .env)
from app.evidence import preresearch  # noqa: E402
from app.evidence.checks import Guarded, Source, normalise, quote_in_source  # noqa: E402
from app.evidence.index import EvidenceIndex, make_embedder  # noqa: E402
from app.evidence.llm import make_verdict_llm  # noqa: E402
from app.evidence.deep import DEEP_TIMEOUT_S, DeepChecker, make_deep_llm, replaces  # noqa: E402
from app.evidence.run import Checker, INDEX_LLM_TIMEOUT_S, Outcome  # noqa: E402
from app.evidence.search import make_search  # noqa: E402
from app.evidence.verdicts import judge  # noqa: E402

GOLD = ROOT / "eval" / "verdict_gold.jsonl"
SAVED = ROOT / "eval" / "saved_evidence.jsonl"
RESULTS = ROOT / "eval" / "verdict_results.tsv"
LABELS = ("supported", "contradicted", "missing_context", "unverifiable")
SPEAKING_CONFIDENCE = 0.75
PASS_RATE = 0.7                # 14 of 20
HOLDOUT_PASS = 3               # of 5
MAX_P95_MS = 2000
MAIN_COUNTS = {"total": 20, "per_label": 5, "per_side": 10, "per_motion": 5, "motions": 4}
SWAP = {("supported", "contradicted"), ("contradicted", "supported")}


def load_gold(path: Path, holdout: bool = False) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if (r.get("set", "main") == "holdout") == holdout]


def balance_problems(rows: list[dict]) -> list[str]:
    """The agreed shape of the main set. Differences are reported, since a lopsided set flatters a timid model."""
    want, out = MAIN_COUNTS, []
    if len(rows) != want["total"]:
        out.append(f"{len(rows)} claims, expected {want['total']}")
    for field_name, expected, names in (("label", want["per_label"], LABELS), ("side", want["per_side"], ("prop", "opp"))):
        counts = Counter(r.get(field_name) for r in rows)
        out += [f"{n}: {counts[n]} claims, expected {expected}" for n in names if counts[n] != expected]
    motions = Counter(r.get("motion") for r in rows)
    if len(motions) != want["motions"]:
        out.append(f"{len(motions)} Motions, expected {want['motions']}")
    out += [f"{m!r}: {c} claims, expected {want['per_motion']}" for m, c in motions.items() if c != want["per_motion"]]
    for r in rows:
        if r.get("label") != "unverifiable" and not (r.get("sources") and r.get("key")):
            out.append(f"{r.get('gid')}: needs accepted sources and a key phrase or number")
    return out


def _url(u: str) -> str:
    return u.split("#")[0].rstrip("/").lower()


def is_hit(row: dict, chunks: list) -> bool:
    accepted = {_url(u) for u in row.get("sources", [])}
    key = normalise(row.get("key", ""))
    for c in chunks:
        url, text = (c.url, c.text) if isinstance(c, Source) else (c["url"], c["text"])
        if _url(url) in accepted or (key and key in normalise(text)):
            return True
    return False


@dataclass
class Result:
    gid: str
    side: str
    gold: str
    label: str
    confidence: float
    fabricated: bool
    rejected: bool
    latency_ms: float
    via: str


def audit(row: dict, guarded: Guarded, sources: list[Source], latency_ms: float, via: str) -> Result:
    """Fabrication is checked here again, independently of the guard that's being measured."""
    fabricated = any(not any(_url(s.url) == _url(e.url) and quote_in_source(e.quote, s.text) for s in sources)
                     for e in guarded.evidence)
    return Result(row["gid"], row.get("side", ""), row["label"], guarded.label, guarded.confidence, fabricated,
                  guarded.quote_rejected, latency_ms, via)


def p95(values: list[float]) -> Optional[float]:
    return sorted(values)[max(0, math.ceil(.95 * len(values)) - 1)] if values else None


def score(results: list[Result]) -> dict:
    timed = [r.latency_ms for r in results if r.via in ("index", "saved")]
    total = len(results)
    correct = [r for r in results if r.label == r.gold]
    return {
        "total": total, "correct": len(correct),
        "fabricated": sum(r.fabricated for r in results),
        "rejected": sum(r.rejected for r in results),
        "swaps_at_speaking_confidence": [r.gid for r in results if (r.gold, r.label) in SWAP and r.confidence >= SPEAKING_CONFIDENCE],
        "unverifiable_rate": sum(r.label == "unverifiable" for r in results) / total if total else 0.0,
        "confusion": Counter((r.gold, r.label) for r in results),
        "by_side": {s: (sum(r.side == s and r.label == r.gold for r in results), sum(r.side == s for r in results)) for s in ("prop", "opp")},
        "p50_ms": statistics.median(timed) if timed else None,
        "p95_ms": p95(timed),
    }


def gate_failures(m: dict, holdout: bool = False) -> list[str]:
    out = []
    if m["fabricated"]:
        out.append(f"{m['fabricated']} fabricated quote(s)")
    if m["swaps_at_speaking_confidence"]:
        out.append(f"supported/contradicted swaps at {SPEAKING_CONFIDENCE}+: {', '.join(m['swaps_at_speaking_confidence'])}")
    needed = HOLDOUT_PASS if holdout else math.ceil(PASS_RATE * m["total"])
    if m["correct"] < needed:
        out.append(f"{m['correct']} of {m['total']} correct, needs {needed}")
    if not holdout and m["p95_ms"] is not None and m["p95_ms"] >= MAX_P95_MS:
        out.append(f"p95 {m['p95_ms']:.0f} ms, needs under {MAX_P95_MS}")
    return out


def report(m: dict) -> str:
    lines = [f"correct {m['correct']}/{m['total']}   fabricated {m['fabricated']}   quotes rejected by the guard {m['rejected']}   "
             f"unverifiable {m['unverifiable_rate']:.0%}",
             "by side: " + "  ".join(f"{s} {c}/{n}" for s, (c, n) in m["by_side"].items())]
    if m["p95_ms"] is not None:
        lines.append(f"latency from index: p50 {m['p50_ms']:.0f} ms, p95 {m['p95_ms']:.0f} ms")
    lines.append("gold \\ got".ljust(18) + "".join(n[:12].ljust(14) for n in LABELS))
    for gold in LABELS:
        lines.append(gold.ljust(18) + "".join(str(m["confusion"][(gold, got)]).ljust(14) for got in LABELS))
    return "\n".join(lines)


def _load_index(motion: str, cache: dict) -> EvidenceIndex:
    if motion not in cache:
        path = preresearch.index_path(motion)
        if not path.with_suffix(".json").exists():
            raise SystemExit(f"No saved index for {motion!r}. Build it: python -m app.evidence.preresearch {motion!r}")
        cache[motion] = EvidenceIndex.load(path)
    return cache[motion]


async def _retrieve(row: dict, cache: dict, embedder) -> list[Source]:
    return await Checker(None, embedder=embedder, index=_load_index(row["motion"], cache), motion=row["motion"]).retrieve(row["text"])


async def layer_retrieval(rows: list[dict]) -> int:
    cache: dict = {}
    embedder, scored = make_embedder(), [r for r in rows if r["label"] != "unverifiable"]
    misses = []
    for r in scored:
        if not is_hit(r, await _retrieve(r, cache, embedder)):
            misses.append(r["gid"])
    hits = len(scored) - len(misses)
    print(f"recall@5: {hits}/{len(scored)} (embedder: {embedder.name if embedder else 'none, BM25 only'})")
    if misses:
        print("misses, check these by hand if they affect a decision:", ", ".join(misses))
    return 0


async def save_evidence(rows: list[dict]) -> int:
    cache: dict = {}
    embedder, missing = make_embedder(), []
    with SAVED.open("w") as f:
        for r in rows:
            sources = await _retrieve(r, cache, embedder)
            if r["label"] != "unverifiable" and not is_hit(r, sources):
                missing.append(r["gid"])
            f.write(json.dumps({"gid": r["gid"], "sources": [vars(s) for s in sources]}) + "\n")
    print(f"saved {len(rows)} rows to {SAVED}")
    if missing:
        print("add the correct passage by hand for:", ", ".join(missing))
    return 0


async def run_verdict_once(rows: list[dict], llm, saved: dict) -> list[Result]:
    out = []
    for r in rows:
        if r["gid"] not in saved:
            raise SystemExit(f"{r['gid']} isn't in {SAVED.name}. Run --save-evidence first.")
        sources = saved[r["gid"]]
        start = time.perf_counter()
        try:
            guarded = await judge(llm, r["text"], r["motion"], sources, INDEX_LLM_TIMEOUT_S)
        except Exception as e:
            guarded = Guarded(explanation=f"error: {e!r}")
        out.append(audit(r, guarded, sources, (time.perf_counter() - start) * 1000, "saved"))
    return out


async def run_full_once(rows: list[dict], llm, searcher, embedder, cache: dict) -> list[Result]:
    out = []
    for r in rows:
        checker = Checker(llm, searcher, embedder, _load_index(r["motion"], cache), r["motion"])
        start = time.perf_counter()
        try:
            outcome = await checker.check(r["text"], web=searcher is not None)
            guarded, sources, via = outcome.guarded, outcome.sources, outcome.via
        except Exception as e:
            guarded, sources, via = Guarded(explanation=f"error: {e!r}"), [], "error"
        out.append(audit(r, guarded, sources, (time.perf_counter() - start) * 1000, via))
    return out


async def run_deep_once(rows: list[dict], llm, searcher, embedder, cache: dict) -> list[Result]:
    """The full check, then the deep check, keeping what the live module would publish last. Latency is the fast
    verdict's, as that's the one the moderator can speak on time."""
    deep_llm, limit, search_limit = make_deep_llm(llm), asyncio.Semaphore(4), asyncio.Semaphore(6)

    async def one(r: dict) -> tuple[Result, str]:
        async with limit:
            checker = Checker(llm, searcher, embedder, _load_index(r["motion"], cache), r["motion"])
            start = time.perf_counter()
            try:
                fast = await checker.check(r["text"], web=searcher is not None)
            except Exception as e:
                fast = Outcome(Guarded(explanation=f"error: {e!r}"), via="error")
            fast_ms, final = (time.perf_counter() - start) * 1000, fast
            try:
                deeper = DeepChecker(deep_llm, searcher, r["motion"], checker.index, search_limit, plan_llm=llm)
                guarded, sources = await asyncio.wait_for(deeper.check(r["text"], fast.sources), DEEP_TIMEOUT_S)
                if replaces(fast.guarded, guarded):
                    final = Outcome(guarded, "deep", sources)
            except Exception as e:
                print(f"  deep check failed for {r['gid']}: {e!r}")
            return audit(r, final.guarded, final.sources, fast_ms, final.via), fast.guarded.label

    pairs = await asyncio.gather(*(one(r) for r in rows))
    changed = [(res, before) for res, before in pairs if res.via == "deep"]
    fixed = [res.gid for res, before in changed if before != res.gold and res.label == res.gold]
    broke = [res.gid for res, before in changed if before == res.gold and res.label != res.gold]
    print(f"deep replaced {len(changed)} verdicts: fixed {len(fixed)} {fixed}, broke {len(broke)} {broke}")
    return [res for res, _ in pairs]


def log_run(layer: str, model: str, m: dict, failures: list[str], note: str) -> None:
    new = not RESULTS.exists()
    with RESULTS.open("a") as f:
        if new:
            f.write("time\tlayer\tmodel\tcorrect\ttotal\tfabricated\tswaps\tp95_ms\tpassed\tnote\n")
        p = f"{m['p95_ms']:.0f}" if m["p95_ms"] is not None else ""
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')}\t{layer}\t{model}\t{m['correct']}\t{m['total']}\t{m['fabricated']}\t"
                f"{len(m['swaps_at_speaking_confidence'])}\t{p}\t{not failures}\t{note}\n")


class Paced:
    """Spaces calls to a rate-limited model and retries when the server says to slow down. The wait for a slot
    isn't counted against a call's timeout, so verdicts still get their full time, but reported latency includes
    the waits. EVAL_LLM_PER_MINUTE=6 suits Kimi K2.6 on Canopy Wave (1 a second, 6 a minute)."""

    def __init__(self, llm, per_minute: float):
        self.llm, self.gap = llm, 60.0 / per_minute
        self.name = getattr(llm, "name", "llm")
        self.next_at, self.lock = 0.0, asyncio.Lock()

    async def _slot(self) -> None:
        async with self.lock:
            wait = self.next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next_at = time.monotonic() + self.gap

    async def complete_json(self, *args, **kwargs):
        for attempt in range(4):
            await self._slot()
            try:
                return await self.llm.complete_json(*args, **kwargs)
            except Exception as e:
                if "429" not in str(e) or attempt == 3:
                    raise
                await asyncio.sleep(self.gap)


def make_eval_llm():
    llm = make_verdict_llm()
    per_minute = float(os.environ.get("EVAL_LLM_PER_MINUTE", 0) or 0)
    return Paced(llm, per_minute) if llm is not None and per_minute > 0 else llm


async def layer_scored(args, rows: list[dict]) -> int:
    llm = make_eval_llm()
    if llm is None:
        raise SystemExit("No verdict model configured: set a key in .env")
    saved, cache, searcher, embedder = {}, {}, None, None
    if args.layer == "verdict":
        if not SAVED.exists():
            raise SystemExit("No saved evidence. Run --save-evidence first.")
        for line in SAVED.read_text().splitlines():
            row = json.loads(line)
            saved[row["gid"]] = [Source(**s) for s in row["sources"]]
    else:
        searcher, embedder = make_search(), make_embedder()
    failed, corrects = False, []
    for n in range(args.runs):
        if args.layer == "verdict":
            results = await run_verdict_once(rows, llm, saved)
        elif args.layer == "deep":
            results = await run_deep_once(rows, llm, searcher, embedder, cache)
        else:
            results = await run_full_once(rows, llm, searcher, embedder, cache)
        m = score(results)
        failures = gate_failures(m, args.holdout)
        corrects.append(m["correct"])
        print(f"\n--- run {n + 1}/{args.runs}: {getattr(llm, 'name', 'llm')}, {args.layer}{', holdout' if args.holdout else ''}")
        print(report(m))
        print("PASS" if not failures else "FAIL: " + "; ".join(failures))
        log_run(args.layer + ("-holdout" if args.holdout else ""), getattr(llm, "name", "llm"), m, failures, args.note)
        failed = failed or bool(failures)
    if args.runs > 1:
        print(f"\ncorrect across runs: {corrects}. A failure in any run fails the model. Gaps under 3 claims are ties.")
    return 1 if failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Score Evidence against the labelled claims")
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--layer", choices=("retrieval", "verdict", "full", "deep"), default="verdict")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--holdout", action="store_true", help="score the fifth Motion instead of the main set")
    ap.add_argument("--check", action="store_true", help="only check the gold set's shape")
    ap.add_argument("--save-evidence", action="store_true")
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)
    if not args.gold.exists():
        raise SystemExit(f"{args.gold} doesn't exist yet. See eval/verdict_gold.example.jsonl for the format.")
    rows = load_gold(args.gold, args.holdout)
    problems = [] if args.holdout else balance_problems(rows)
    for p in problems:
        print("gold set:", p)
    if args.check:
        print("gold set looks right" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if args.save_evidence:
        return asyncio.run(save_evidence(load_gold(args.gold) + load_gold(args.gold, True)))
    if args.layer == "retrieval":
        return asyncio.run(layer_retrieval(rows))
    return asyncio.run(layer_scored(args, rows))


if __name__ == "__main__":
    raise SystemExit(main())
