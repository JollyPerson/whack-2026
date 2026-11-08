"""Evidence: Claim -> Verdict + SourceCard, index first and web as the fallback. Owner: C.

Indexes are built in advance (see preresearch.py), or on the fly from the stage's setup screen: a ResearchRequest
(Motion + brief) builds and saves the index and its fact sheet without touching the running session, and the
SessionStart that follows loads it, or waits for the build already in flight. A Motion nobody prepared is researched
live in the background, and claims that arrive meanwhile go straight to the web.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from app import config, latency
from app.evidence import preresearch
from app.evidence.checks import Guarded, Source
from app.evidence.index import EvidenceIndex, chunk_text, make_embedder
from app.evidence.llm import make_verdict_llm
from app.evidence.search import make_search
from app.evidence.verdicts import judge
from app.schemas import Claim, FactSheet, ResearchProgress, SourceCard, Verdict

log = logging.getLogger("evidence")

TOP_K = 5
INDEX_LLM_TIMEOUT_S = float(os.environ.get("EVIDENCE_LLM_TIMEOUT_S", 3.0))
WEB_SEARCH_TIMEOUT_S = float(os.environ.get("EVIDENCE_WEB_TIMEOUT_S", 3.0))
QUERY_EMBED_TIMEOUT_S = float(os.environ.get("EVIDENCE_EMBED_TIMEOUT_S", 1.0))   # then BM25 alone
CONCURRENCY = int(os.environ.get("EVIDENCE_CONCURRENCY", 6))
FACT_K = 2                 # matching fact-sheet entries put in front of the index results


@dataclass
class Outcome:
    """What a check produced, with everything the eval needs to audit it."""
    guarded: Guarded
    via: str = "none"
    sources: list[Source] = field(default_factory=list)
    retrieved_at: Optional[float] = None


class Checker:
    def __init__(self, llm, searcher=None, embedder=None, index: Optional[EvidenceIndex] = None, motion: str = "",
                 facts: Optional[list[dict]] = None):
        self.llm, self.searcher, self.embedder = llm, searcher, embedder
        self.index, self.motion = index, motion
        self.set_facts(facts or [])

    def set_facts(self, facts: list[dict]) -> None:
        """Facts are searched by statement + quote; the source handed to the judge is the chunk the quote came from."""
        self.facts = [f for f in facts if f.get("context")]
        self.fact_index = EvidenceIndex([{"url": f["url"], "title": f.get("title", ""), "text": f"{f['statement']} {f['quote']}",
                                          "context": f["context"]} for f in self.facts]) if self.facts else None

    def fact_sources(self, claim: str) -> list[Source]:
        if not self.fact_index:
            return []
        return [Source(c["url"], c["title"], c["context"]) for c in self.fact_index.search(claim, FACT_K)]

    async def retrieve(self, claim: str) -> list[Source]:
        facts = self.fact_sources(claim)
        if not self.index or not len(self.index):
            return facts
        vector = None
        if self.embedder is not None and self.index.vectors is not None:
            try:
                vector = (await asyncio.wait_for(asyncio.to_thread(self.embedder.embed, [claim]), QUERY_EMBED_TIMEOUT_S))[0]
            except Exception as e:
                log.warning("query embedding failed, using BM25: %r", e)
        found = [Source(c["url"], c["title"], c["text"]) for c in self.index.search(claim, TOP_K, vector)]
        seen = {s.text for s in facts}
        return facts + [s for s in found if s.text not in seen][:TOP_K - len(facts) // 2]

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
        self.session = 0
        self.limit = asyncio.Semaphore(CONCURRENCY)
        self.tasks: set[asyncio.Task] = set()
        self.builds: dict[str, asyncio.Task] = {}   # index path -> build in flight (prepare and SessionStart share it)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _progress(self, stage: str, done: int, total: int, detail: str = "", motion: str = "") -> None:
        self.bus.publish(ResearchProgress(stage=stage, done=done, total=total, detail=detail, motion=motion), src="evidence")

    def _fact_sheet(self, motion: str, brief: str, facts: list[dict]) -> None:
        public = [{k: v for k, v in f.items() if k != "context"} for f in facts]
        self.bus.publish(FactSheet(motion=motion, brief=brief, facts=public), src="evidence")

    async def _load(self, path) -> tuple[EvidenceIndex, list[dict]]:
        index = await asyncio.to_thread(EvidenceIndex.load, path)
        return index, await asyncio.to_thread(preresearch.load_facts, path)

    def _build(self, motion: str, brief: str, deadline_s: float) -> asyncio.Task:
        """One build per Motion + brief: a second request (or the SessionStart after a prepare) joins it."""
        key = str(preresearch.index_path(motion, brief=brief))
        if key not in self.builds:
            self.builds[key] = asyncio.create_task(self._build_and_save(motion, brief, deadline_s))
            self.builds[key].add_done_callback(lambda _t: self.builds.pop(key, None))
        return self.builds[key]

    async def _build_and_save(self, motion: str, brief: str, deadline_s: float) -> tuple[EvidenceIndex, list[dict]]:
        def progress(stage, done, total, detail=""):
            if stage != "ready":            # "ready" only once the fact sheet is done too
                self._progress(stage, done, total, detail, motion)
        index = await preresearch.build_index(motion, self.searcher, self.llm, self.embedder, progress,
                                              deadline_s=deadline_s, brief=brief)
        facts = await preresearch.extract_facts(motion, index, self.llm, brief, progress=progress)
        path = preresearch.index_path(motion, brief=brief)
        if len(index):                      # never cache an empty index: the next try should search again
            await asyncio.to_thread(index.save, path)
            await asyncio.to_thread(preresearch.save_facts, path, facts)
        self._progress("ready", len(index), len(index), f"{len(index)} passages, {len(facts)} key facts", motion)
        self._fact_sheet(motion, brief, facts)
        return index, facts

    async def prepare(self, motion: str, brief: str = "") -> None:
        """ResearchRequest: research before the debate, leaving the running session alone."""
        if not motion.strip():
            return
        path = preresearch.index_path(motion, brief=brief)
        if path.with_suffix(".json").exists():
            index, facts = await self._load(path)
            self._progress("ready", len(index), len(index), "built in advance", motion)
            self._fact_sheet(motion, brief, facts)
            return
        if self.searcher is None:
            self._progress("error", 0, 0, "Live research needs EXA_API_KEY; checks will use saved indexes only", motion)
            return
        try:
            await self._build(motion, brief, preresearch.PREPARE_DEADLINE_S)
        except Exception as e:
            log.warning("pre-research failed: %r", e)
            self._progress("error", 0, 0, "Pre-research failed; checks will use web search only", motion)

    async def start_session(self, motion: str, brief: str = "") -> None:
        self.session += 1
        session = self.session
        self.checker = Checker(self.llm, self.searcher, self.embedder, motion=motion)
        path = preresearch.index_path(motion, brief=brief)
        if not path.with_suffix(".json").exists() and brief.strip() and str(path) not in self.builds:
            plain = preresearch.index_path(motion)     # a demo Motion with a brief added still has its saved index
            if plain.with_suffix(".json").exists():
                path = plain
        if path.with_suffix(".json").exists():
            self.checker.index, facts = await self._load(path)
            self.checker.set_facts(facts)
            self._progress("ready", len(self.checker.index), len(self.checker.index), "built in advance", motion)
            self._fact_sheet(motion, brief, facts)
            return
        if not motion or self.searcher is None:
            self._progress("error", 0, 0, "No saved index, and live research isn't available", motion)
            return
        try:
            index, facts = await self._build(motion, brief, preresearch.LIVE_DEADLINE_S)
        except Exception as e:
            log.warning("pre-research failed: %r", e)
            self._progress("error", 0, 0, "Pre-research failed, using web search only", motion)
            return
        if session == self.session:
            self.checker.index = index
            self.checker.set_facts(facts)

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
        async for ev in self.bus.subscribe("evidence", {"Claim", "SessionStart", "ResearchRequest"}):
            if ev.event == "SessionStart":
                self._spawn(self.start_session(ev.motion, ev.brief))
            elif ev.event == "ResearchRequest":
                self._spawn(self.prepare(ev.motion, ev.brief))
            elif ev.checkable:
                self._spawn(self.handle_claim(ev))


async def run(bus) -> None:
    await EvidenceModule(bus, make_verdict_llm(), make_search(), make_embedder()).run()
