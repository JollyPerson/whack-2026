"""Mock brain: regex claims + a naive Director, no API keys. Owner: B (stand-in for the real brain,
so C and D can develop even if app/brain is broken). Deliberately independent of app/brain."""
from __future__ import annotations

import itertools
import re
import time

from app.schemas import ArgMapSnapshot, Claim, DirectorDecision, Interjection, Link, MapEdge, MapNode

NUM = re.compile(r"\d|per ?cent|million|billion|half|third|twice|double", re.I)
REBUT = re.compile(r"^(but|no|actually|that's not|that is not|wrong)\b", re.I)
SPLIT = re.compile(r"(?<=[.!?])\s+")


async def run(bus):
    ids, iids = itertools.count(1), itertools.count(1)
    claims: dict[str, Claim] = {}
    edges: list[MapEdge] = []
    verdicts: dict[str, object] = {}
    last_fire, version = 0.0, itertools.count(1)
    sides: dict[str, str] = {}

    def snapshot():
        bus.publish(ArgMapSnapshot(version=next(version),
                                   nodes=[MapNode(claim_id=c.id, speaker=c.speaker, side=c.side, text=c.text,
                                                  type=c.type, importance=c.importance,
                                                  verdict=getattr(verdicts.get(c.id), "label", None))
                                          for c in claims.values()],
                                   edges=edges), src="mock_brain")

    async for ev in bus.subscribe("mock_brain", {"Utterance", "Pause", "Verdict", "SessionStart"}):
        if ev.event == "SessionStart":
            claims.clear(); edges.clear(); verdicts.clear()
            sides = {k: v.side for k, v in ev.speakers.items()}
        elif ev.event == "Utterance" and ev.final:
            for sent in SPLIT.split(ev.text):
                if len(sent.split()) < 4:
                    continue
                stat = bool(NUM.search(sent))
                links = []
                if REBUT.search(sent):
                    other = [c for c in claims.values() if c.speaker != ev.speaker]
                    if other:
                        links = [Link(claim_id=other[-1].id, relation="attacks")]
                c = Claim(id=f"c{next(ids)}", utterance_id=ev.id, speaker=ev.speaker, text=sent.strip(),
                          type="statistic" if stat else "other", checkable=stat, links=links,
                          side=sides.get(ev.speaker), importance=0.8 if stat else 0.4, source="mock",
                          marks={"speech_end": ev.t_end or ev.ts, "stt_final": ev.ts, "claim": time.time()})
                claims[c.id] = c
                edges += [MapEdge(src=c.id, dst=l.claim_id, relation=l.relation) for l in links]
                bus.publish(c, src="mock_brain")
            snapshot()
        elif ev.event == "Verdict":
            if ev.claim_id not in claims:  # e.g. a check that outlived its session: nothing on screen to correct
                continue
            verdicts[ev.claim_id] = ev
            snapshot()
        elif ev.event == "Pause":
            now = time.time()
            recent = [v for v in verdicts.values() if v.label == "contradicted" and now - v.ts < 20]
            if recent and now - last_fire > 30:
                v = recent[-1]
                last_fire = now
                bus.publish(DirectorDecision(key=f"correction:{v.claim_id}", kind="correction", claim_id=v.claim_id,
                                             action="fired", reason="mock"), src="mock_brain")
                bus.publish(Interjection(id=f"i-{next(iids):04d}", text="Point of fact: the source on screen disagrees.",
                                         priority=0.8, claim_id=v.claim_id, kind="correction",
                                         target=claims[v.claim_id].speaker if v.claim_id in claims else None,
                                         evidence=v.evidence[:1]), src="mock_brain")
