"""Deep check: a slower second look at each claim, in the background. Owner: C.

The fast check (index, then a quick web search) has seconds. This one has about 30: the model rewrites the spoken
claim into written search queries that name the document likely to settle it, Exa searches with full text both
within primary sources and on the open web, and the verdict model judges more passages with more effort.

The result is a second Verdict with via="deep". CONTRACTS.md: a later Verdict for a claim replaces the earlier one
on screen and in the map, and the Director may still speak it if nothing was said about that claim yet. It's only
published when it improves on the fast verdict (see replaces()).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from app import config
from app.evidence.checks import Guarded, Source
from app.evidence.index import EvidenceIndex, chunk_text
from app.evidence.preresearch import PRIMARY_DOMAINS
from app.evidence.verdicts import judge

log = logging.getLogger("evidence.deep")

DEEP_TIMEOUT_S = float(os.environ.get("EVIDENCE_DEEP_TIMEOUT_S", 30.0))
DEEP_CONCURRENCY = int(os.environ.get("EVIDENCE_DEEP_CONCURRENCY", 2))
DEEP_MAX_WAITING = int(os.environ.get("EVIDENCE_DEEP_MAX_WAITING", 6))   # beyond this, skip: a backlog delays all
DEEP_MIN_IMPORTANCE = float(os.environ.get("EVIDENCE_DEEP_MIN_IMPORTANCE", 0.5))
DEEP_TOP_K = 10
QUERIES = 4
PLAN_TIMEOUT_S = 8.0
SEARCH_TIMEOUT_S = 12.0
JUDGE_TIMEOUT_S = 18.0
JUDGE_MAX_TOKENS = 4000        # medium effort thinks for longer, and thinking counts toward max_tokens
RESULTS_PER_SEARCH = 5
PAGE_CHARS = 12000

QUERY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["queries"],
                "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}
QUERY_SYSTEM = (f"A debater has just said the claim below aloud, in a UK debate on the Motion given. Write up to "
                f"{QUERIES} web search queries that would find the primary source that settles it. Name the specific "
                "report, dataset or study, with its publisher and year, when you can. Write figures the way "
                "documents do (12%, £3.8bn), not the way people say them. Don't assume the claim is true or false. "
                "Return JSON only.")


def wants_deep(checkable: bool, importance: float) -> bool:
    return checkable and importance >= DEEP_MIN_IMPORTANCE


def replaces(fast: Guarded, deep: Guarded) -> bool:
    """Whether the deep verdict should be published over the fast one.

    Failing to find a source again never undoes a verdict whose quote was verified. Otherwise the more thorough
    check wins, unless it disagrees and is less sure than the fast one."""
    if deep.label == "unverifiable":
        return False
    if fast.label == "unverifiable":
        return True
    if deep.label == fast.label:
        return deep.confidence > fast.confidence
    return deep.confidence >= fast.confidence


class DeepChecker:
    def __init__(self, llm, searcher, motion: str = "", index: Optional[EvidenceIndex] = None,
                 search_limit: Optional[asyncio.Semaphore] = None, plan_llm=None):
        self.llm, self.searcher, self.motion, self.index = llm, searcher, motion, index
        self.plan_llm = plan_llm or llm
        self.search_limit = search_limit or asyncio.Semaphore(6)   # Exa allows 10 queries a second, shared

    async def plan(self, claim: str) -> list[str]:
        """Written search queries for the claim. The claim itself is always one of them."""
        queries = [claim]
        try:
            got = await self.plan_llm.complete_json(QUERY_SYSTEM, f"Motion: {self.motion}\n\nClaim: {claim}",
                                                    QUERY_SCHEMA, timeout=PLAN_TIMEOUT_S, max_tokens=600)
            queries += [q.strip() for q in got.get("queries", []) if isinstance(q, str) and q.strip()][:QUERIES]
        except Exception as e:
            log.warning("deep query planning failed, searching the claim alone: %r", e)
        return list(dict.fromkeys(queries))

    async def search(self, queries: list[str]) -> list[dict]:
        """Pages for every query, within primary sources and on the open web. Primary pages come first."""
        async def one(query: str, domains) -> list[dict]:
            async with self.search_limit:
                try:
                    return await self.searcher.search(query, kind="auto", results=RESULTS_PER_SEARCH,
                                                      max_chars=PAGE_CHARS, timeout=SEARCH_TIMEOUT_S,
                                                      include_domains=domains)
                except Exception as e:
                    log.warning("deep search failed for %r: %r", query, e)
                    return []
        jobs = [(q, PRIMARY_DOMAINS) for q in queries] + [(q, None) for q in queries]
        results = await asyncio.gather(*(one(q, d) for q, d in jobs))
        pages, seen = [], set()
        for found in results:
            for page in found:
                if page["url"] not in seen:
                    seen.add(page["url"])
                    pages.append(page)
        return pages

    @staticmethod
    def rank(claim: str, queries: list[str], pages: list[dict], earlier: list[Source]) -> list[Source]:
        """The best passages from the new pages and the fast check's sources, by BM25 fused across every query.
        The written queries match how documents phrase a figure, which the spoken claim often doesn't."""
        chunks = [{"url": p["url"], "title": p["title"], "text": c} for p in pages for c in chunk_text(p["text"])]
        chunks += [{"url": s.url, "title": s.title, "text": s.text} for s in earlier]
        ranked = EvidenceIndex(chunks).search(claim, DEEP_TOP_K, also=[q for q in queries if q != claim])
        return [Source(c["url"], c["title"], c["text"]) for c in ranked]

    async def check(self, claim: str, earlier: list[Source]) -> tuple[Guarded, list[Source]]:
        """The deep verdict and the passages it was judged on. Raises if the verdict model fails."""
        queries = await self.plan(claim)
        pages = await self.search(queries) if self.searcher is not None else []
        if self.index is not None and len(self.index):     # the Motion's index again, now with the written queries
            earlier = earlier + [Source(c["url"], c["title"], c["text"])
                                 for c in self.index.search(claim, DEEP_TOP_K, also=queries[1:])]
        sources = self.rank(claim, queries, pages, earlier)
        if not sources:
            return Guarded(explanation="The deep check found no source either."), []
        guarded = await judge(self.llm, claim, self.motion, sources, JUDGE_TIMEOUT_S, max_tokens=JUDGE_MAX_TOKENS)
        return guarded, sources


def make_deep_llm(llm):
    """The verdict model at medium effort, sharing its client. Other clients are used as they are."""
    from app.brain.llm import AnthropicLLM
    if isinstance(llm, AnthropicLLM):
        return AnthropicLLM(llm.client, model=llm.model, effort=os.environ.get("EVIDENCE_DEEP_EFFORT", "medium"))
    return llm


def enabled() -> bool:
    return bool(config.FLAGS.get("deep_check", False))
