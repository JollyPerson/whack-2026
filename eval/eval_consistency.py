"""Score the consistency checker on the planted cases in fixtures/consistency_debate.jsonl. Owner: B.

  python -m eval.eval_consistency                 # the heuristic: free, no key, no network
  python -m eval.eval_consistency --llm           # the model (CONSISTENCY_MODEL, else LLM_MODEL = Haiku). COSTS TOKENS
  python -m eval.eval_consistency --llm --runs 3  # sampling isn't pinned: report the spread
  python -m eval.eval_consistency -v              # print every finding

Each fixture sentence becomes one claim (claim extraction is bypassed, so this scores the checker alone), turns
are checked in order exactly as the brain does, and findings are matched to eval/consistency_gold.jsonl by kind,
side and the gold `now`/`then` snippets appearing in the two claims. A finding on a "none" row (the rhetorical
"even if") is a false accusation. The target is zero false self-contradictions: a wrong charge of hypocrisy on screen
is worse than a missed one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.argmap import ArgMap  # noqa: E402
from app.brain.consistency import ConsistencyChecker, make_consistency_llm, norm  # noqa: E402
from app.schemas import Claim, SessionStart, Utterance  # noqa: E402

FIXTURE = ROOT / "fixtures" / "consistency_debate.jsonl"
GOLD = ROOT / "eval" / "consistency_gold.jsonl"


def build(fixture: Path) -> ArgMap:
    amap, n = ArgMap(), 0
    for line in fixture.read_text().splitlines():
        e = json.loads(line)
        if e["event"] == "SessionStart":
            amap.reset(SessionStart.model_validate(e))
        elif e["event"] == "Utterance":
            n += 1
            u = Utterance.model_validate({**e, "ts": e["ts"] + 1e9})
            amap.on_utterance(u)
            amap.add_claim(Claim(id=f"c{n}", utterance_id=u.id, speaker=u.speaker, text=u.text, importance=.67, ts=u.ts))
    return amap


def matches(gold: dict, k, amap: ArgMap) -> bool:
    now, then = amap.nodes[k.claim_id].claim.text, amap.nodes[k.earlier_claim_id].claim.text
    return (k.side == gold["side"] and norm(gold["now"]) in norm(now)
            and (not gold["then"] or norm(gold["then"]) in norm(then)))


async def one_run(llm, verbose: bool) -> dict:
    amap = build(FIXTURE)
    out: list = []
    checker = ConsistencyChecker(amap, out.append, llm, delay_s=0)
    t0 = time.monotonic()
    for t in amap.turns:
        await checker.check_turn(t.idx)
    secs = time.monotonic() - t0
    gold = [json.loads(x) for x in GOLD.read_text().splitlines() if x.strip()]
    hit, false_charge, used = 0, 0, set()
    for g in gold:
        found = [k for k in out if matches(g, k, amap)]
        if g["kind"] == "none":
            false_charge += len(found)
            used |= {k.id for k in found}
            continue
        good = [k for k in found if k.kind == g["kind"]]
        hit += bool(good)
        used |= {k.id for k in good[:1]}
    extra = [k for k in out if k.id not in used]
    false_u = sum(k.kind == "self_contradiction" for k in extra)
    real = sum(g["kind"] != "none" for g in gold)
    if verbose:
        for k in out:
            print(f"  {k.kind:18} {k.side:4} {k.confidence:.2f}  now: {amap.nodes[k.claim_id].claim.text[:60]!r}\n"
                  f"  {'':18} {'':4} {'':4}  then: {amap.nodes[k.earlier_claim_id].claim.text[:60]!r}")
    return {"recall": hit / real, "found": hit, "real": real, "extra": len(extra), "false_u_turns": false_u,
            "rhetorical_taken": false_charge, "calls": checker.stats["calls"], "seconds": round(secs, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--llm", action="store_true", help="use the model (costs tokens); default: the free heuristic")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    llm = None
    if a.llm:
        from app import config  # noqa: F401  (loads .env)
        from app.brain.llm import make_llm, usage_line
        llm = make_consistency_llm(make_llm())
        if llm is None:
            raise SystemExit("--llm: no model is configured (set ANTHROPIC_API_KEY)")
        print(f"model: {getattr(llm, 'name', llm)}")
    for i in range(a.runs):
        r = asyncio.run(one_run(llm, a.verbose))
        print(f"run {i + 1}: found {r['found']}/{r['real']} planted cases, {r['extra']} extra findings, "
              f"{r['false_u_turns']} false U-turns, rhetorical 'even if' taken as a concession: {r['rhetorical_taken']}, "
              f"{r['calls']} model calls, {r['seconds']} s")
    if llm is not None:
        print("usage:", usage_line({"consistency": llm}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
