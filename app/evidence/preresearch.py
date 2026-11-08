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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.evidence.index import EvidenceIndex, build_vectors, chunk_text, make_embedder

log = logging.getLogger("evidence.preresearch")

INDEX_DIR = Path(os.environ.get("EVIDENCE_INDEX_DIR", "cache/evidence"))
MOTIONS_FILE = Path(__file__).with_name("motions.json")
RESULTS_PER_QUERY = int(os.environ.get("PRERESEARCH_RESULTS", 10))
# The primary-source pass searches within these sites (subdomains included). Their pages are kept first, so the
# chunk cap drops commentary before it drops an official report.
PRIMARY_DOMAINS = [
    "gov.uk", "ons.gov.uk", "parliament.uk", "nao.org.uk", "ofcom.org.uk", "ofsted.gov.uk", "nhs.uk",
    "bankofengland.co.uk", "obr.uk", "ifs.org.uk", "fullfact.org", "resolutionfoundation.org", "instituteforgovernment.org.uk",
    "kingsfund.org.uk", "nuffieldtrust.org.uk", "ac.uk", "nih.gov", "doi.org", "springer.com", "sciencedirect.com",
    "nature.com", "thelancet.com", "bmj.com", "wiley.com", "oecd.org", "who.int", "europa.eu", "un.org", "worldbank.org",
    "imf.org", "bbc.co.uk/aboutthebbc", "downloads.bbc.co.uk", "tfl.gov.uk", "orr.gov.uk",
]
QUERY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["queries"],
                "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}
BROAD_QUERIES = ("You plan web research for a neutral UK debate fact-checker. Given a Motion, write {n} search queries "
                 "for the checkable facts each side is likely to cite: official statistics, government and regulator "
                 "reports, academic evidence, and notable case studies. Cover both sides equally. Prefer UK sources, "
                 "and add international evidence where the topic needs it. Return JSON only.")
DOCUMENT_QUERIES = ("You plan web research for a neutral UK debate fact-checker. Given a Motion, write {n} search "
                    "queries for the checkable facts each side is likely to cite. Cover both sides equally. At least "
                    "half the queries should name a specific primary document or dataset, for example an annual "
                    "report, a statistical release, an accounts or trust statement, a regulator's report, a government "
                    "evaluation or a peer-reviewed study, by its usual title and year. The rest can target official "
                    "statistics, academic evidence and notable case studies. Prefer UK sources, and add international "
                    "evidence where the topic needs it. Return JSON only.")


@dataclass(frozen=True)
class Profile:
    queries: int
    primary_pass: bool       # run each query a second time within PRIMARY_DOMAINS
    max_chars: int           # per page; long reports keep their key figures deep inside
    max_chunks: int
    query_prompt: str


# Scored on the verdict eval (Opus, index only, 3 runs each): about 600 pages per Motion with vectors and 8 chunks,
# 18/20; the same pages on BM25 alone, 12-14/20, below the original 180 pages on BM25 (17/20). So the big build
# is only used when an embedder can rank it, and BM25-only builds stay as they were.
WITH_VECTORS = Profile(queries=40, primary_pass=True, max_chars=8000, max_chunks=8000, query_prompt=DOCUMENT_QUERIES)
BM25_ONLY = Profile(queries=20, primary_pass=False, max_chars=4000, max_chunks=2000, query_prompt=BROAD_QUERIES)


def profile_for(embedder) -> Profile:
    """The build for this embedder. PRERESEARCH_QUERIES, _PRIMARY (0/1), _MAX_CHARS and _MAX_CHUNKS override it."""
    base = WITH_VECTORS if embedder is not None else BM25_ONLY
    env = os.environ.get
    return replace(base, queries=int(env("PRERESEARCH_QUERIES", base.queries)),
                   primary_pass=env("PRERESEARCH_PRIMARY", "1" if base.primary_pass else "0") == "1",
                   max_chars=int(env("PRERESEARCH_MAX_CHARS", base.max_chars)),
                   max_chunks=int(env("PRERESEARCH_MAX_CHUNKS", base.max_chunks)))


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


async def plan_queries(motion: str, llm, profile: Profile = BM25_ONLY) -> list[str]:
    if llm is not None:
        try:
            got = await llm.complete_json(profile.query_prompt.format(n=profile.queries), f"Motion: {motion}",
                                          QUERY_SCHEMA, timeout=20.0, max_tokens=2000)
            queries = list(dict.fromkeys(q.strip() for q in got.get("queries", []) if isinstance(q, str) and q.strip()))
            if queries:
                return queries[:profile.queries]
        except Exception as e:
            log.warning("query planning failed, using templates: %r", e)
    return template_queries(motion)


Progress = Callable[[str, int, int, str], None]


async def build_index(motion: str, searcher, llm=None, embedder=None, progress: Optional[Progress] = None,
                      deadline_s: float = 90.0) -> EvidenceIndex:
    """Search, chunk and embed. The deadline cuts remaining searches; earlier results are kept and earlier queries win."""
    report = progress or (lambda *a: None)
    start = time.monotonic()
    profile = profile_for(embedder)
    queries = await plan_queries(motion, llm, profile)
    jobs = ([(q, PRIMARY_DOMAINS) for q in queries] if profile.primary_pass else []) + [(q, None) for q in queries]
    report("searching", 0, len(jobs), motion)
    limit = asyncio.Semaphore(8)                # Exa allows 10 queries a second
    done = 0

    async def one(query: str, domains: Optional[list[str]]) -> list[dict]:
        nonlocal done
        async with limit:
            try:
                remaining = max(1.0, deadline_s - (time.monotonic() - start))
                return await searcher.search(query, kind="auto", results=RESULTS_PER_QUERY, max_chars=profile.max_chars,
                                             timeout=min(15.0, remaining), include_domains=domains)
            except Exception as e:
                log.warning("search failed for %r: %r", query, e)
                return []
            finally:
                done += 1
                report("searching", done, len(jobs), query)

    tasks = [asyncio.create_task(one(q, d)) for q, d in jobs]
    await asyncio.wait(tasks, timeout=deadline_s * .7)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    pages, seen = [], set()
    for t in tasks:                             # primary-source pages first, then the open searches
        if not t.cancelled() and not t.exception():
            for page in t.result():
                if page["url"] not in seen:
                    seen.add(page["url"])
                    pages.append(page)
    chunks = [{"url": p["url"], "title": p["title"], "text": c} for p in pages for c in chunk_text(p["text"])][:profile.max_chunks]
    report("embedding", 0, len(chunks), f"{len(pages)} pages")
    vectors = await asyncio.to_thread(build_vectors, chunks, embedder)
    index = EvidenceIndex(chunks, vectors, embedder.name if embedder and vectors is not None else "")
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

    async def go() -> None:
        for motion in motions:
            print(motion, flush=True)
            index = await build_index(motion, searcher, llm, embedder, show, deadline_s=float(os.environ.get("PRERESEARCH_DEADLINE_S", 240)))
            path = index_path(motion)
            index.save(path)
            print(f"  saved {len(index)} chunks to {path} (embedder: {index.embedder_name or 'none, BM25 only'})", flush=True)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
