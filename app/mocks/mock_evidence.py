"""Mock evidence: canned, deterministic verdicts after a fake delay. Owner: B (stand-in for C).

Label and delay come from a hash of the claim text, so the same fixture always gives the same
verdicts (stable demos and tests). On SessionStart and ResearchRequest it fakes pre-research progress and a
FactSheet built from the Motion's words, so the stage's setup screen works with no keys.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

from app import latency
from app.schemas import Evidence, Fact, FactSheet, ResearchProgress, SourceCard, Verdict

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


def mock_facts(motion: str) -> list[Fact]:
    topic = motion.removeprefix("This house would ").rstrip(".") or "the motion"
    url = "https://www.ons.gov.uk/mock/factsheet"
    rows = [("prop", f"[mock] A 2023 review found {topic} cut costs by 12% in pilot areas.", 12, "%"),
            ("opp", f"[mock] Surveys show 58% of residents opposed plans to {topic}.", 58, "%"),
            ("both", f"[mock] Around 1.4 million people would be directly affected if we {topic}.", 1.4e6, "people"),
            ("prop", f"[mock] Three of five comparable countries that tried to {topic} kept the policy.", 3, "countries"),
            ("opp", f"[mock] Official estimates put the up-front cost to {topic} at £2.1bn.", 2.1e9, "GBP")]
    return [Fact(statement=s, side=side, quote=s[7:60], url=url, title="Mock ONS bulletin", number=n, unit=u)
            for side, s, n, u in rows]


async def fake_research(bus, motion: str, brief: str = "", step: float = 0.2):
    for i in range(0, 21, 4):
        bus.publish(ResearchProgress(stage="searching", done=i, total=20, motion=motion, detail="mock search"),
                    src="mock_evidence")
        await asyncio.sleep(step)
    bus.publish(ResearchProgress(stage="ready", done=20, total=20, motion=motion, detail="20 passages, 5 key facts"),
                src="mock_evidence")
    bus.publish(FactSheet(motion=motion, brief=brief, facts=mock_facts(motion)), src="mock_evidence")


async def run(bus):
    async for ev in bus.subscribe("mock_evidence", {"Claim", "SessionStart", "ResearchRequest"}):
        if ev.event == "SessionStart":
            asyncio.create_task(fake_research(bus, ev.motion, ev.brief))
        elif ev.event == "ResearchRequest":
            asyncio.create_task(fake_research(bus, ev.motion, ev.brief, step=0.6))
        elif ev.checkable:
            asyncio.create_task(check(bus, ev))
