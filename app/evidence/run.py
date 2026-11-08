"""Evidence: Claim -> Verdict + SourceCard, index first and web as the fallback. Owner: C.

Indexes are built in advance (see preresearch.py). A Motion without one is researched live in the background, and
claims that arrive meanwhile go straight to the web. After the fast verdict, important claims get a slower deep
check in the background (deep.py), which publishes a second Verdict when it does better. A claim already settled
in an earlier debate on the Motion is answered from the verdict cache (cache.py) without checking it again.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app import config, latency
from app.evidence import deep, preresearch
from app.evidence.cache import VerdictCache
from app.evidence.checks import Guarded, Source
from app.evidence.index import EvidenceIndex, chunk_text, make_embedder
from app.evidence.llm import make_verdict_llm
from app.evidence.search import make_search
from app.evidence.verdicts import judge
from app.schemas import Claim, ResearchProgress, SourceCard, Verdict

log = logging.getLogger("evidence")

# Chunks shown to the verdict model. With vectors, 8 scored best on the eval (18/20 vs 16/20 at 5); BM25 alone does
# better at 5, as extra keyword matches are mostly noise.
TOP_K = int(os.environ.get("EVIDENCE_TOP_K", 5))
TOP_K_WITH_VECTORS = int(os.environ.get("EVIDENCE_TOP_K_VECTORS", 8))
INDEX_LLM_TIMEOUT_S = float(os.environ.get("EVIDENCE_LLM_TIMEOUT_S", 4.0))   # Opus verdicts: p95 about 3.3 s
WEB_SEARCH_TIMEOUT_S = float(os.environ.get("EVIDENCE_WEB_TIMEOUT_S", 3.0))
QUERY_EMBED_TIMEOUT_S = float(os.environ.get("EVIDENCE_EMBED_TIMEOUT_S", 1.0))   # then BM25 alone
CONCURRENCY = int(os.environ.get("EVIDENCE_CONCURRENCY", 6))


@dataclass
class Outcome:
    """What a check produced, with everything the eval needs to audit it."""
    guarded: Guarded
    via: str = "none"
    sources: list[Source] = field(default_factory=list)
    retrieved_at: Optional[float] = None


class Checker:
    def __init__(self, llm, searcher=None, embedder=None, index: Optional[EvidenceIndex] = None, motion: str = ""):
        self.llm, self.searcher, self.embedder = llm, searcher, embedder
        self.index, self.motion = index, motion

    async def retrieve(self, claim: str) -> list[Source]:
        if not self.index or not len(self.index):
            return []
        vector = None
        if self.embedder is not None and self.index.vectors is not None:
            try:
                vector = (await asyncio.wait_for(asyncio.to_thread(self.embedder.embed, [claim]), QUERY_EMBED_TIMEOUT_S))[0]
            except Exception as e:
                log.warning("query embedding failed, using BM25: %r", e)
        k = TOP_K_WITH_VECTORS if vector is not None else TOP_K
        return [Source(c["url"], c["title"], c["text"]) for c in self.index.search(claim, k, vector)]

    async def web_sources(self, claim: str) -> list[Source]:
        pages = await self.searcher.search(claim, kind="lite", results=5, max_chars=2500, timeout=WEB_SEARCH_TIMEOUT_S)
        ranked = EvidenceIndex([{"url": p["url"], "title": p["title"], "text": c}
                                for p in pages for c in chunk_text(p["text"])]).search(claim, TOP_K)
        return [Source(c["url"], c["title"], c["text"]) for c in ranked]

    async def check(self, claim: str, web: bool = True) -> Outcome:
        if self.llm is None:
            return Outcome(Guarded(explanation="No verdict model is configured."))
        outcome = Outcome(Guarded(explanation="No source settles this."))
        sources = await self.retrieve(claim)
        outcome.retrieved_at = time.time()
        if sources:
            outcome = await self._judged(claim, sources, "index", outcome.retrieved_at)
        if outcome.guarded.label == "unverifiable" and web and self.searcher is not None:
            try:
                web_found = await self.web_sources(claim)
            except Exception as e:
                log.warning("web fallback failed: %r", e)
                return outcome
            if web_found:
                outcome = await self._judged(claim, web_found, "web", outcome.retrieved_at)
        return outcome

    async def _judged(self, claim: str, sources: list[Source], via: str, retrieved_at: Optional[float]) -> Outcome:
        guarded = await judge(self.llm, claim, self.motion, sources, INDEX_LLM_TIMEOUT_S)
        return Outcome(guarded, via, sources, retrieved_at)


def to_verdict(claim: Claim, outcome: Outcome) -> Verdict:
    g = outcome.guarded
    verdict = Verdict(claim_id=claim.id, label=g.label, confidence=g.confidence, evidence=g.evidence, via=outcome.via,
                      explanation=g.explanation, short_fact=g.short_fact, numeric=g.numeric, guard_notes=g.notes)
    if outcome.retrieved_at:
        latency.stamp(verdict, "retrieved", outcome.retrieved_at)
    return latency.finish(verdict, claim)


class EvidenceModule:
    def __init__(self, bus, llm=None, searcher=None, embedder=None):
        self.bus, self.llm, self.searcher, self.embedder = bus, llm, searcher, embedder
        self.checker = Checker(llm, searcher, embedder)
        self.verdicts = VerdictCache()
        self.session = 0
        self.limit = asyncio.Semaphore(CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()
        self.deep_llm = deep.make_deep_llm(llm) if llm is not None else None
        self.deep_limit = asyncio.Semaphore(deep.DEEP_CONCURRENCY)
        self.deep_search_limit = asyncio.Semaphore(6)    # shared by every deep check: Exa allows 10 queries a second
        self.deep_waiting = 0

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _progress(self, stage: str, done: int, total: int, detail: str = "") -> None:
        self.bus.publish(ResearchProgress(stage=stage, done=done, total=total, detail=detail), src="evidence")

    async def start_session(self, motion: str) -> None:
        self.session += 1
        session = self.session
        self.checker = Checker(self.llm, self.searcher, self.embedder, motion=motion)
        self.verdicts = VerdictCache()
        verdicts = await asyncio.to_thread(VerdictCache.for_motion, motion)
        if session == self.session:
            self.verdicts = verdicts
        path = preresearch.index_path(motion)
        if path.with_suffix(".json").exists():
            self.checker.index = await asyncio.to_thread(EvidenceIndex.load, path)
            self._progress("ready", len(self.checker.index), len(self.checker.index), "built in advance")
            return
        if not motion or self.searcher is None:
            self._progress("error", 0, 0, "No saved index, and live research isn't available")
            return
        try:
            # No embedder for a live build: embedding a big build takes 20+ minutes on the server's single vCPU,
            # mid-debate. The small BM25 build is what scored best without vectors anyway.
            index = await preresearch.build_index(motion, self.searcher, self.llm, None, self._progress)
        except Exception as e:
            log.warning("pre-research failed: %r", e)
            self._progress("error", 0, 0, "Pre-research failed, using web search only")
            return
        if session == self.session:
            self.checker.index = index
            await asyncio.to_thread(index.save, path)

    async def handle_claim(self, claim: Claim) -> None:
        session, checker, verdicts = self.session, self.checker, self.verdicts
        if config.FLAGS.get("verdict_cache", True) and (hit := verdicts.lookup(claim.text)) is not None:
            self._publish(claim, Outcome(hit.guarded, "cache", retrieved_at=time.time()))
            return
        async with self.limit:
            try:
                if claim.t_end > 1e9 and time.time() - claim.t_end > config.STALE_CLAIM_S:   # below 1e9 isn't a real clock time
                    outcome = Outcome(Guarded(explanation="Too old to check live."), via="stale")
                else:
                    outcome = await asyncio.wait_for(checker.check(claim.text, web=bool(config.FLAGS.get("web_fallback", True))),
                                                     config.VERDICT_TIMEOUT_S)
            except asyncio.TimeoutError:
                outcome = Outcome(Guarded(explanation="Ran out of time."), via="timeout")
            except Exception as e:
                log.warning("check failed for %s: %r", claim.id, e)
                outcome = Outcome(Guarded(explanation="The check failed."), via="error")
        if session != self.session:
            return                              # the debate moved on to a new Motion
        self._publish(claim, outcome)
        await asyncio.to_thread(verdicts.add, claim.text, outcome.guarded, outcome.via)
        if self.deep_llm is not None and deep.enabled() and deep.wants_deep(claim.checkable, claim.importance):
            self._spawn(self.deepen(claim, outcome, session, checker))

    def _publish(self, claim: Claim, outcome: Outcome) -> None:
        verdict = to_verdict(claim, outcome)
        for e in verdict.evidence:
            self.bus.publish(SourceCard(claim_id=claim.id, url=e.url, title=e.title, snippet=e.quote), src="evidence")
        self.bus.publish(verdict, src="evidence")

    async def deepen(self, claim: Claim, fast: Outcome, session: int, checker: Checker) -> None:
        """The deep check for one claim. Publishes only if it beats the fast verdict and the Motion hasn't changed."""
        if self.deep_waiting >= deep.DEEP_MAX_WAITING:
            log.info("deep check skipped for %s: %d already waiting", claim.id, self.deep_waiting)
            return
        self.deep_waiting += 1
        waiting = True
        try:
            async with self.deep_limit:
                self.deep_waiting, waiting = self.deep_waiting - 1, False
                checking = deep.DeepChecker(self.deep_llm, self.searcher, checker.motion, checker.index,
                                            self.deep_search_limit, plan_llm=self.llm)
                started = time.time()
                guarded, sources = await asyncio.wait_for(checking.check(claim.text, fast.sources), deep.DEEP_TIMEOUT_S)
        except Exception as e:                  # includes running out of time: the fast verdict stands
            log.warning("deep check failed for %s: %r", claim.id, e)
            return
        finally:
            if waiting:                         # cancelled before a slot came free
                self.deep_waiting -= 1
        if session != self.session or not deep.replaces(fast.guarded, guarded):
            return
        self._publish(claim, Outcome(guarded, "deep", sources, started))
        await asyncio.to_thread(self.verdicts.add, claim.text, guarded, "deep")

    async def run(self) -> None:
        async for ev in self.bus.subscribe("evidence", {"Claim", "SessionStart"}):
            if ev.event == "SessionStart":
                self._spawn(self.start_session(ev.motion))
            elif ev.checkable:
                self._spawn(self.handle_claim(ev))


async def run(bus) -> None:
    await EvidenceModule(bus, make_verdict_llm(), make_search(), make_embedder()).run()
