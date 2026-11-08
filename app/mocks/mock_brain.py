"""Mock brain: regex claims + a naive Director, no API keys. Owner: B (stand-in for the real brain,
so C and D can develop even if app/brain is broken). Deliberately independent of app/brain.

Also simulates the consistency checker with word rules (no model): a concession marker ("I'll grant", "my opponent is
right") -> concession of the other side's closest earlier claim, unless it's an "even if"; "we both agree" ->
agreement; the same side giving a different figure for something it mentioned before -> self_contradiction. On
Control screen=final (or summary) it publishes a DebateSummary built from those findings. Try it with
FIXTURE=fixtures/consistency_debate.jsonl ./run.sh mock"""
from __future__ import annotations

import itertools
import re
import time

from app.schemas import (ArgMapSnapshot, Claim, Consistency, DebateSummary, DirectorDecision, DisputedPoint, Interjection,
                         Link, MapEdge, MapNode, SummaryPoint)

NUM = re.compile(r"\d|per ?cent|million|billion|half|third|twice|double", re.I)
REBUT = re.compile(r"^(but|no|actually|that's not|that is not|wrong)\b", re.I)
CONCEDE = re.compile(r"\b(i(?:'ll| will)? (?:grant|accept|concede|admit)|opponent is right|you(?:'re| are) right|"
                     r"fair point|admittedly|granted)\b", re.I)
AGREE = re.compile(r"\b(we (?:both |all )?agree|both sides agree|common ground)\b", re.I)
IFFY = re.compile(r"^\s*(?:but |and )?(?:even if|suppose|assuming|if)\b", re.I)
QTY = re.compile(r"\d[\d,.]*|\b(?:nothing|half|third|quarter|fifth|tenth|double|twice|one|two|three|four|five|ten|"
                 r"hundred|thousand|million|billion)\b", re.I)
_W = re.compile(r"[a-z]+")
_STOP = set("the a an and or but of to in on at for by with from as is are was were be been it its this that we our "
            "you your they their would will can could should not no more most much many some any all only about into "
            "than then so just also very around nearly almost per cent percent i ll grant opponent right agree both "
            "even if anyway still need needs".split())


def _words(text: str, ignore=frozenset()) -> set:
    out = set()
    for w in _W.findall(text.lower()):
        if len(w) > 2 and w not in _STOP and not QTY.fullmatch(w):
            w = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            if w[:5] not in ignore:
                out.add(w[:5])
    return out


def _qty(text: str) -> set:
    return {q.lower().replace(",", "") for q in QTY.findall(text)}


def simulate_consistency(c: Claim, earlier: list, motion: str = "") -> list[dict]:
    """Findings for claim `c` against `earlier` claims (oldest first), by word rules. Returns dicts for Consistency."""
    ignore = {w for w in _words(motion)}
    mine, out = _words(c.text, ignore), []
    others = [e for e in earlier if e.side != c.side]
    if AGREE.search(c.text) or (CONCEDE.search(c.text) and not IFFY.search(c.text)):
        best = max(others, key=lambda e: len(mine & _words(e.text, ignore)), default=None)
        if best is not None and mine & _words(best.text, ignore):
            kind = "agreement" if AGREE.search(c.text) else "concession"
            out.append(dict(kind=kind, earlier=best, conf=.82,
                            why="Both sides now hold this position." if kind == "agreement"
                            else "The speaker accepts the other side's earlier point."))
    q = _qty(c.text)
    if q:
        for e in reversed([e for e in earlier if e.side == c.side]):
            eq = _qty(e.text)
            if eq and not (q & eq) and mine & _words(e.text, ignore):
                out.append(dict(kind="self_contradiction", earlier=e, conf=.78,
                                why="The same side gave a different figure for this earlier."))
                break
    return out


def simulate_summary(motion: str, claims: dict, findings: list, verdicts: dict) -> DebateSummary:
    agreed, used = [], set()
    for k in findings:
        if k.kind in ("agreement", "concession") and k.earlier_claim_id in claims:
            used |= {k.earlier_claim_id, k.claim_id}
            agreed.append(SummaryPoint(statement=claims[k.earlier_claim_id].text, claim_ids=[k.earlier_claim_id, k.claim_id]))
    rest = {s: [c for c in claims.values() if c.side == s and c.id not in used] for s in ("prop", "opp")}
    disputed = []
    for p, o in zip(sorted(rest["prop"], key=lambda c: -c.importance), sorted(rest["opp"], key=lambda c: -c.importance)):
        lean = {"prop": 0, "opp": 0}
        for c in (p, o):
            v = verdicts.get(c.id)
            if v is not None:
                lean[c.side] += {"supported": 1, "contradicted": -1}.get(v.label, 0)
        disputed.append(DisputedPoint(issue=" ".join(p.text.split()[:5]) + "…", prop=p.text, opp=o.text,
                                      evidence_leans="prop" if lean["prop"] > lean["opp"] else
                                      "opp" if lean["opp"] > lean["prop"] else "even", claim_ids=[p.id, o.id]))
        if len(disputed) == 4:
            break
    score = {}
    for s in ("prop", "opp"):
        mine = [verdicts[c.id].label for c in claims.values() if c.side == s and c.id in verdicts]
        sup, con, mis = mine.count("supported"), mine.count("contradicted"), mine.count("missing_context")
        score[s] = round((sup + .5 * mis + 1) / (sup + con + mis + 2), 3)
    return DebateSummary(motion=motion, agreed=agreed[:5], disputed=disputed, source="mock", score=score,
                         self_contradictions={s: sum(k.kind == "self_contradiction" and k.side == s for k in findings)
                                              for s in ("prop", "opp")},
                         concessions={s: sum(k.kind == "concession" and k.side == s for k in findings)
                                      for s in ("prop", "opp")},
                         call="[mock] the evidence " + ("favours the proposition" if score["prop"] > score["opp"] else
                                                        "favours the opposition" if score["opp"] > score["prop"] else
                                                        "is level"))

SPLIT = re.compile(r"(?<=[.!?])\s+")


async def run(bus):
    ids, iids, kids = itertools.count(1), itertools.count(1), itertools.count(1)
    findings: list = []
    motion = ""
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

    async for ev in bus.subscribe("mock_brain", {"Utterance", "Pause", "Verdict", "SessionStart", "Control"}):
        if ev.event == "Control":
            if (ev.flag == "screen" and ev.value == "final") or ev.flag == "summary":
                bus.publish(simulate_summary(motion, claims, findings, verdicts), src="mock_brain")
            continue
        if ev.event == "SessionStart":
            claims.clear(); edges.clear(); verdicts.clear(); findings.clear()
            motion = ev.motion
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
                earlier = list(claims.values())
                claims[c.id] = c
                edges += [MapEdge(src=c.id, dst=l.claim_id, relation=l.relation) for l in links]
                bus.publish(c, src="mock_brain")
                for f in simulate_consistency(c, earlier, motion):
                    e = f["earlier"]
                    k = Consistency(id=f"k-{next(kids):04d}", kind=f["kind"], claim_id=c.id, earlier_claim_id=e.id,
                                    side=c.side, speaker=c.speaker, earlier_side=e.side, now=c.text, then=e.text,
                                    explanation=f["why"], confidence=f["conf"], topic=" ".join(c.text.split()[:4]))
                    findings.append(k)
                    bus.publish(k, src="mock_brain")
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
