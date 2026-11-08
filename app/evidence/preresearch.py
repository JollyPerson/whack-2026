"""Build the evidence index for a Motion. Owner: C.

Demo Motions are built in advance:  python -m app.evidence.preresearch --all --missing
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.evidence.index import EvidenceIndex, build_vectors, chunk_text, make_embedder

log = logging.getLogger("evidence.preresearch")

INDEX_DIR = Path(os.environ.get("EVIDENCE_INDEX_DIR", "cache/evidence"))
MOTIONS_FILE = Path(__file__).with_name("motions.json")
MAX_CHUNKS = 2000       # about the most a local model can embed in the time; see the search research
QUERY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["queries"],
                "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}
QUERY_SYSTEM = ("You plan web research for a neutral UK debate fact-checker. Given a Motion, write 20 search queries "
                "for the checkable facts each side is likely to cite: official statistics, government and regulator "
                "reports, academic evidence, and notable case studies. Cover both sides equally. Prefer UK sources, "
                "and add international evidence where the topic needs it. Return JSON only.")


def demo_motions() -> list[str]:
    return [m["text"] for m in json.loads(MOTIONS_FILE.read_text())["motions"]]


def index_path(motion: str, root: Optional[Path] = None) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", motion.lower()).strip("-")[:60] or "motion"
    digest = hashlib.sha1(" ".join(motion.lower().split()).encode()).hexdigest()[:8]
    return (root or INDEX_DIR) / f"{slug}-{digest}"


def template_queries(motion: str) -> list[str]:
    return [f"{motion} {suffix}" for suffix in (
        "evidence", "UK official statistics", "arguments for", "arguments against", "government report",
        "academic research", "case study", "cost and benefits", "public opinion survey", "international comparison")]


async def plan_queries(motion: str, llm) -> list[str]:
    if llm is not None:
        try:
            got = await llm.complete_json(QUERY_SYSTEM, f"Motion: {motion}", QUERY_SCHEMA, timeout=8.0, max_tokens=900)
            queries = [q.strip() for q in got.get("queries", []) if isinstance(q, str) and q.strip()]
            if queries:
                return queries[:20]
        except Exception as e:
            log.warning("query planning failed, using templates: %r", e)
    return template_queries(motion)


Progress = Callable[[str, int, int, str], None]


async def build_index(motion: str, searcher, llm=None, embedder=None, progress: Optional[Progress] = None,
                      deadline_s: float = 90.0) -> EvidenceIndex:
    """Search, chunk and embed. The deadline cuts remaining searches; earlier results are kept and earlier queries win."""
    report = progress or (lambda *a: None)
    start = time.monotonic()
    queries = await plan_queries(motion, llm)
    report("searching", 0, len(queries), motion)
    limit = asyncio.Semaphore(8)                # Exa allows 10 queries a second
    done = 0

    async def one(query: str) -> list[dict]:
        nonlocal done
        async with limit:
            try:
                remaining = max(1.0, deadline_s - (time.monotonic() - start))
                return await searcher.search(query, kind="auto", timeout=min(15.0, remaining))
            except Exception as e:
                log.warning("search failed for %r: %r", query, e)
                return []
            finally:
                done += 1
                report("searching", done, len(queries), query)

    tasks = [asyncio.create_task(one(q)) for q in queries]
    try:
        await asyncio.wait(tasks, timeout=deadline_s * .7)
    finally:                                    # also when this build is cancelled: wait() leaves its tasks running
        for t in tasks:
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    pages, seen = [], set()
    for t in tasks:
        if not t.cancelled() and not t.exception():
            for page in t.result():
                if page["url"] not in seen:
                    seen.add(page["url"])
                    pages.append(page)
    chunks = [{"url": p["url"], "title": p["title"], "text": c} for p in pages for c in chunk_text(p["text"])][:MAX_CHUNKS]
    report("embedding", 0, len(chunks), f"{len(pages)} pages")
    vectors = await asyncio.to_thread(build_vectors, chunks, embedder)
    index = EvidenceIndex(chunks, vectors, embedder.name if embedder and vectors is not None else "")
    if chunks:                                  # an empty index isn't ready: the caller reports the error
        report("ready", len(chunks), len(chunks), f"{len(pages)} pages")
    return index


def main() -> int:
    from app import config  # noqa: F401  (loads .env)
    from app.evidence.llm import make_verdict_llm
    from app.evidence.search import make_search

    ap = argparse.ArgumentParser(description="Build and save the evidence index for each Motion")
    ap.add_argument("motions", nargs="*")
    ap.add_argument("--all", action="store_true", help="every Motion in motions.json")
    ap.add_argument("--missing", action="store_true", help="skip Motions that already have a saved index")
    args = ap.parse_args()
    motions = args.motions + (demo_motions() if args.all else [])
    if args.missing:
        motions = [m for m in motions if not index_path(m).with_suffix(".json").exists()]
    if not motions:
        print("Nothing to build.")
        return 0
    searcher = make_search()
    if searcher is None:
        raise SystemExit("EXA_API_KEY isn't set")
    llm, embedder = make_verdict_llm(), make_embedder()

    def show(stage: str, done: int, total: int, detail: str) -> None:
        print(f"  {stage} {done}/{total} {detail[:70]}", flush=True)

    async def go() -> int:
        empty = 0
        for motion in motions:
            print(motion, flush=True)
            index = await build_index(motion, searcher, llm, embedder, show, deadline_s=float(os.environ.get("PRERESEARCH_DEADLINE_S", 240)))
            if not len(index):                  # saving it would replace a good index, or make --missing skip it
                empty += 1
                print("  nothing found, not saved: check EXA_API_KEY and the search quota", flush=True)
                continue
            path = index_path(motion)
            index.save(path)
            print(f"  saved {len(index)} chunks to {path} (embedder: {index.embedder_name or 'none, BM25 only'})", flush=True)
        return empty

    return 1 if asyncio.run(go()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
