"""Mock evidence: canned, deterministic verdicts after a fake delay. Owner: B (stand-in for C).

Label and delay come from a hash of the claim text, so the same fixture always gives the same
verdicts (stable demos and tests). On SessionStart it fakes pre-research progress.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

from app import latency
from app.schemas import Evidence, ResearchProgress, SourceCard, Verdict

LABELS = ["supported", "contradicted", "missing_context", "unverifiable"]


def _h(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest(), 16)


async def check(bus, c):
    h = _h(c.text)
    delay = float(os.environ.get("MOCK_EVIDENCE_DELAY", 0.8 + (h % 1700) / 1000))
    await asyncio.sleep(delay)
    label = LABELS[h % 4]
    url = f"https://www.ons.gov.uk/mock/{c.id}"
    bus.publish(SourceCard(claim_id=c.id, url=url, title="Mock ONS bulletin", snippet=f"... {c.text[:80]} ..."),
                src="mock_evidence")
    v = Verdict(claim_id=c.id, label=label, confidence=0.8 + (h % 15) / 100, via="index",
                evidence=[Evidence(url=url, title="Mock ONS bulletin", quote=c.text[:60])],
                explanation=f"[mock] {label}", short_fact="the official figure differs")
    bus.publish(latency.finish(v, c), src="mock_evidence")


async def run(bus):
    async for ev in bus.subscribe("mock_evidence", {"Claim", "SessionStart"}):
        if ev.event == "SessionStart":
            async def progress():
                for i in range(0, 21, 4):
                    bus.publish(ResearchProgress(stage="ready" if i >= 20 else "searching", done=i, total=20),
                                src="mock_evidence")
                    await asyncio.sleep(0.2)
            asyncio.create_task(progress())
        elif ev.checkable:
            asyncio.create_task(check(bus, ev))
