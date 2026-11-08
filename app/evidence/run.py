"""Evidence: Claim -> Verdict + SourceCard, index first and web as the fallback. Owner: C.

Indexes are built in advance (see preresearch.py). A Motion without one is researched live in the background, and
claims that arrive meanwhile go straight to the web.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app import config, latency
from app.brain.llm import LLMError, LLMTimeout
from app.evidence import preresearch
from app.evidence.checks import Guarded, Source
from app.evidence.index import EvidenceIndex, chunk_text, make_embedder
from app.evidence.llm import make_verdict_llm
from app.evidence.search import make_search
from app.evidence.verdicts import judge
from app.schemas import Claim, ResearchProgress, SourceCard, Verdict

log = logging.getLogger("evidence")

TOP_K = 5
INDEX_LLM_TIMEOUT_S = float(os.environ.get("EVIDENCE_LLM_TIMEOUT_S", 3.0))
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
        vector, index = None, self.index
        if self.embedder is not None and index.vectors is not None and getattr(self.embedder, "name", None) == index.embedder_name:
            try:                                # another model's vectors would be meaningless, or the wrong size
                vector = (await asyncio.wait_for(asyncio.to_thread(self.embedder.embed, [claim]), QUERY_EMBED_TIMEOUT_S))[0]
            except Exception as e:
                log.warning("query embedding failed, using BM25: %r", e)
        return [Source(c["url"], c["title"], c["text"]) for c in self.index.search(claim, TOP_K, vector)]

    async def web_sources(self, claim: str) -> list[Source]:
        pages = await self.searcher.search(claim, kind="lite", results=5, max_chars=2500, timeout=WEB_SEARCH_TIMEOUT_S)
        ranked = EvidenceIndex([{"url": p["url"], "title": p["title"], "text": c}
                                for p in pages for c in chunk_text(p["text"])]).search(claim, TOP_K)
        return [Source(c["url"], c["title"], c["text"]) for c in ranked]

    async def check(self, claim: str, web: bool = True) -> Outcome:
        if self.llm is None:
            return Outcome(Guarded(explanation="No verdict model is configured."))
        outcome, failed = Outcome(Guarded(explanation="No source settles this.")), None
        sources = await self.retrieve(claim)
        outcome.retrieved_at = time.time()
        if sources:
            try:
                outcome = await self._judged(claim, sources, "index", outcome.retrieved_at)
            except LLMTimeout:
                raise                           # too little of the budget is left for the web
            except LLMError as e:               # a refusal or a cut-short answer: other sources may still settle it
                log.warning("index verdict failed, trying the web: %r", e)
                failed = e
        if outcome.guarded.label == "unverifiable" and web and self.searcher is not None:
            try:
                web_found = await self.web_sources(claim)
            except Exception as e:
                log.warning("web fallback failed: %r", e)
                web_found = []
            if web_found:
                return await self._judged(claim, web_found, "web", outcome.retrieved_at)
        if failed is not None:
            raise failed                        # nothing else answered: still a failed check, not "unverifiable"
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
        self.session = 0
        self.limit = asyncio.Semaphore(CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("evidence task failed", exc_info=task.exception())

    def _progress(self, stage: str, done: int, total: int, detail: str = "") -> None:
        self.bus.publish(ResearchProgress(stage=stage, done=done, total=total, detail=detail), src="evidence")

    async def start_session(self, motion: str) -> None:
        self.session += 1
        session = self.session
        checker = self.checker = Checker(self.llm, self.searcher, self.embedder, motion=motion)

        def progress(stage: str, done: int, total: int, detail: str = "") -> None:
            if session == self.session:         # once a newer Motion starts, the research line is its own
                self._progress(stage, done, total, detail)

        path = preresearch.index_path(motion)
        if path.with_suffix(".json").exists():
            try:
                index = await asyncio.to_thread(EvidenceIndex.load, path)
            except Exception as e:
                log.warning("saved index for %r is unreadable, researching it again: %r", motion, e)
            else:
                if len(index):                  # empty: every search failed when it was built, so try again
                    if index.vectors is not None and getattr(self.embedder, "name", "") != index.embedder_name:
                        log.warning("index for %r was embedded with %r, so retrieval uses BM25 alone",
                                    motion, index.embedder_name)
                    checker.index = index
                    progress("ready", len(index), len(index), "built in advance")
                    return
        if not motion or self.searcher is None:
            progress("error", 0, 0, "No saved index, and live research isn't available")
            return
        try:
            index = await preresearch.build_index(motion, self.searcher, self.llm, self.embedder, progress)
        except Exception as e:
            log.warning("pre-research failed: %r", e)
            progress("error", 0, 0, "Pre-research failed, using web search only")
            return
        if session != self.session:
            return
        if not len(index):                      # not saved, so the next session tries again
            progress("error", 0, 0, "Pre-research found nothing, using web search only")
            return
        checker.index = index
        try:
            await asyncio.to_thread(index.save, path)
        except Exception as e:
            log.warning("couldn't save the index for %r: %r", motion, e)

    async def handle_claim(self, claim: Claim) -> None:
        session, checker = self.session, self.checker
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
        verdict = to_verdict(claim, outcome)
        for e in verdict.evidence:
            self.bus.publish(SourceCard(claim_id=claim.id, url=e.url, title=e.title, snippet=e.quote), src="evidence")
        self.bus.publish(verdict, src="evidence")

    async def run(self) -> None:
        starting: Optional[asyncio.Task] = None
        async for ev in self.bus.subscribe("evidence", {"Claim", "SessionStart"}):
            if ev.event == "SessionStart":
                if starting is not None:
                    starting.cancel()           # the old Motion's research would only spend Exa's rate limit
                starting = self._spawn(self.start_session(ev.motion))
            elif ev.checkable:
                self._spawn(self.handle_claim(ev))


async def run(bus) -> None:
    await EvidenceModule(bus, make_verdict_llm(), make_search(), make_embedder()).run()
