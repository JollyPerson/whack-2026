"""Score claim extraction against hand-labelled gold claims. Owner: B.

  python -m eval.eval_extract                         # fixture + gold defaults; LLM if configured, else heuristic
  python -m eval.eval_extract --heuristic             # score the regex fallback (no key needed)
  python -m eval.eval_extract --runs 3 --swap-sides   # LLM: sampling isn't pinned, so report the spread;
                                                      # swap PROP/OPP to check per-speaker balance
  python -m eval.eval_extract -v                      # list misses, false positives and wrong types

Matching (design §8): a predicted claim matches a gold claim from the same utterance when every number in the
gold text appears in the prediction and token-F1 >= 0.5 (greedy, best pairs first). Prints claim P/R/F1,
checkable P/R/F1, type accuracy, eligibility agreement, link P/R/F1, per-sentence LLM p50/p95, fallback rate,
cache-hit rate and claims/min. Uses the real ArgMap when it is implemented, else a minimal stand-in.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bus import load_jsonl  # noqa: E402
from app.brain.claims import ClaimExtractor  # noqa: E402
from app.brain.prompts import SYSTEM_PROMPT  # noqa: E402
from app.schemas import Claim, SessionStart, SpeakerInfo, Utterance  # noqa: E402

TOK = re.compile(r"[a-z]+|\d+(?:\.\d+)?")


def _numfix(s: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", s.lower())


def toks(s: str) -> set:
    return set(TOK.findall(_numfix(s)))


def nums(s: str) -> set:
    return set(re.findall(r"\d+(?:\.\d+)?", _numfix(s)))


def f1(a: str, b: str) -> float:
    x, y = toks(a), toks(b)
    k = len(x & y)
    return 2 * k / (len(x) + len(y)) if k else 0.0


def prf(tp: int, npred: int, ngold: int) -> tuple:
    p, r = tp / max(npred, 1), tp / max(ngold, 1)
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def pct(xs: list, q: float):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def match(pred: list[dict], gold: list[dict]) -> dict:
    """pred id -> gold gid."""
    cand = sorted(((f1(p["text"], g["text"]), p["id"], g["gid"]) for p in pred for g in gold
                   if p["utterance_id"] == g["utterance_id"] and nums(g["text"]) <= nums(p["text"])), reverse=True)
    m, used = {}, set()
    for s, pid, gid in cand:
        if s >= 0.5 and pid not in m and gid not in used:
            m[pid] = gid
            used.add(gid)
    return m


class MiniMap:
    """Just enough ArgMap for the extractor (used only while app.brain.argmap is a stub)."""

    def __init__(self):
        self.reset()

    def reset(self, session: SessionStart | None = None) -> None:
        self.motion = session.motion if session else ""
        self.sides = {k: v.side for k, v in (session.speakers if session else {}).items()}
        self.claims: list[Claim] = []
        self.turn: tuple = (None, [])

    def side_of(self, speaker: str) -> str:
        if speaker not in self.sides:
            self.sides[speaker] = "prop" if len(self.sides) % 2 == 0 else "opp"
        return self.sides[speaker]

    def on_utterance(self, u: Utterance) -> None:
        spk, items = self.turn
        self.turn = (u.speaker, (items if spk == u.speaker else []) + [(u.id, u.text)])

    def on_pause(self, p) -> None:
        pass

    def add_claim(self, c: Claim) -> None:
        self.claims.append(c)

    def context_for(self, speaker: str, limit: int = 15) -> list[Claim]:
        return self.claims[-limit:]

    def turn_text(self, speaker: str, exclude_utterance_id: str | None = None) -> list[str]:
        spk, items = self.turn
        return [t for i, t in items if i != exclude_utterance_id] if spk == speaker else []

    def find_duplicate(self, speaker: str, text: str):
        for c in reversed(self.claims[-30:]):
            if c.speaker == speaker and nums(c.text) == nums(text):
                a, b = toks(c.text), toks(text)
                if a and b and len(a & b) / len(a | b) >= 0.7:
                    return c.id
        return None

    def merge_repeat(self, claim_id: str, links) -> None:
        pass


def make_map(session: SessionStart):
    try:
        from app.brain.argmap import ArgMap
        m = ArgMap(k_turns=1)
        m.reset(session)
        m.side_of(next(iter(session.speakers), "A"))
        m.context_for(next(iter(session.speakers), "A"))
        return m, "argmap"
    except Exception:                               # stub or half-built ArgMap: score extraction alone
        m = MiniMap()
        m.reset(session)
        return m, "minimap"


def _call(obj, name: str, *args) -> None:
    try:
        getattr(obj, name)(*args)
    except (NotImplementedError, AttributeError):
        pass


async def run_once(events: list, gold: list[dict], llm, swap: bool = False) -> dict:
    session = next((e for e in events if e.event == "SessionStart"), SessionStart())
    if swap:
        flip = {"prop": "opp", "opp": "prop"}
        session = session.model_copy(update={"speakers": {k: SpeakerInfo(name=v.name, side=flip.get(v.side, v.side))
                                                          for k, v in session.speakers.items()}})
    amap, map_kind = make_map(session)
    published: list[Claim] = []
    cur = {"t": 0.0}
    ex = ClaimExtractor(amap, published.append, llm, clock=lambda: cur["t"] or time.time())
    if llm is not None:
        await ex.warmup(session.motion)              # compiles the schema + writes the cache (not scored)
    utts = [e for e in events if e.event == "Utterance" and e.final]

    async def run_units(units):
        for unit in units:                           # sequential => reproducible context
            cur["t"] = unit.t_end or unit.ts         # speech is "fresh" for the stale check
            await ex.extract(unit)

    for ev in events:
        await run_units(ex.flush_due(ev.ts or getattr(ev, "t_end", 0.0)))
        if ev.event == "Utterance" and ev.final:
            _call(amap, "on_utterance", ev)
            await run_units(ex.assemble(ev, ev.ts or ev.t_end))
        elif ev.event == "Pause":
            await run_units(ex.on_pause(ev.speaker, ev.ts or ev.t))
            _call(amap, "on_pause", ev)
    await run_units(ex.flush())

    P = [c.model_dump() for c in published]
    gby = {g["gid"]: g for g in gold}
    m = match(P, gold)
    pc, gc = [p for p in P if p["checkable"]], [g for g in gold if g["checkable"]]
    gl = {(g["gid"], ln["to"], ln["rel"]) for g in gold for ln in g["links"]}
    pl = [(m.get(p["id"]), m.get(ln["claim_id"]), ln["relation"]) for p in P for ln in p["links"]]
    matched = [p for p in P if p["id"] in m]
    span = (max(u.t_end for u in utts) - min(u.t_start for u in utts)) / 60.0 if utts else 0.0
    s = ex.stats
    usage = getattr(llm, "usage", None) or {}
    return {
        "claim": prf(len(m), len(P), len(gold)),
        "checkable": prf(sum(1 for p in pc if p["id"] in m and gby[m[p["id"]]]["checkable"]), len(pc), len(gc)),
        "type_acc": float(statistics.mean(gby[m[p["id"]]]["type"] == p["type"] for p in matched)) if matched else 0.0,
        "eligible_acc": float(statistics.mean((p["importance"] >= 0.5) == (gby[m[p["id"]]]["importance"] >= 2)
                                              for p in matched)) if matched else 0.0,
        "link": prf(len(set(pl) & gl), len(pl), len(gl)),
        "p50_ms": s.get("p50_ms"), "p95_ms": s.get("p95_ms"),
        "fallback": s["fallbacks"] / max(s["calls"], 1), "by_cause": {k: v for k, v in s["by_cause"].items() if v},
        "cache_hit": usage["cache_hits"] / usage["calls"] if usage.get("calls") else None,
        "claims_per_min": len(P) / span if span else 0.0,
        "n_pred": len(P), "n_gold": len(gold), "map": map_kind,
        "per_speaker": {spk: {"claims": sum(p["speaker"] == spk for p in P),
                              "checkable": sum(p["speaker"] == spk and p["checkable"] for p in P),
                              "types": dict(Counter(p["type"] for p in P if p["speaker"] == spk))}
                        for spk in sorted({u.speaker for u in utts})},
        "_pred": P, "_match": m,
    }


def _fmt(v) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def summarize(runs: list[dict]) -> list[tuple[str, str]]:
    def agg(get) -> str:
        xs = [get(r) for r in runs]
        if any(x is None for x in xs):
            return "-"
        if len(xs) == 1:
            return _fmt(xs[0])
        return f"{statistics.mean(xs):.2f} ({min(xs):.2f}..{max(xs):.2f})"

    rows = []
    for name in ("claim", "checkable", "link"):
        for i, lab in enumerate("PRF"):
            rows.append((f"{name} {lab}", agg(lambda r, n=name, i=i: r[n][i])))
    for key in ("type_acc", "eligible_acc", "p50_ms", "p95_ms", "fallback", "cache_hit", "claims_per_min", "n_pred"):
        rows.append((key, agg(lambda r, k=key: r[k])))
    rows.append(("by_cause", json.dumps(runs[-1]["by_cause"])))
    return rows


def explain(run: dict, gold: list[dict]) -> None:
    gby, m = {g["gid"]: g for g in gold}, run["_match"]
    hit = set(m.values())
    for g in gold:
        if g["gid"] not in hit:
            print(f"  MISS {g['gid']:4} {g['utterance_id']}  {g['text']}")
    for p in run["_pred"]:
        if p["id"] not in m:
            print(f"  FP   {p['id']:4} {p['utterance_id']}  {p['text']}")
        else:
            g = gby[m[p["id"]]]
            if g["type"] != p["type"] or g["checkable"] != p["checkable"]:
                print(f"  DIFF {p['id']:4}->{g['gid']:4} type {p['type']}/{g['type']} "
                      f"checkable {p['checkable']}/{g['checkable']}  {p['text']}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fixture", nargs="?", default=str(ROOT / "fixtures/fake_debate.jsonl"))
    ap.add_argument("gold", nargs="?", default=str(ROOT / "eval/gold_claims.jsonl"))
    ap.add_argument("--heuristic", action="store_true", help="score the regex fallback, never call an LLM")
    ap.add_argument("--runs", type=int, default=0, help="default: 1 heuristic, 3 LLM")
    ap.add_argument("--swap-sides", action="store_true", help="also run with PROP/OPP swapped (balance check)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("--tsv", help="append one result row to this TSV file")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    events = load_jsonl(Path(a.fixture))
    gold = [json.loads(line) for line in Path(a.gold).read_text().splitlines() if line.strip()]
    llm = None
    if not a.heuristic:
        from app.brain.llm import make_llm
        llm = make_llm()
        if llm is None:
            print("note: no LLM configured (LLM_PROVIDER / API key) - scoring the heuristic", file=sys.stderr)
    name = getattr(llm, "name", None) or "heuristic"
    n = a.runs or (3 if llm else 1)
    runs = [await run_once(events, gold, llm) for _ in range(n)]
    rows = summarize(runs)
    tag = f"{hashlib.sha1(SYSTEM_PROMPT.encode()).hexdigest()[:8]}:{name}"
    out = {"extractor": name, "prompt": tag, "runs": n, "map": runs[0]["map"], "metrics": dict(rows),
           "per_speaker": runs[-1]["per_speaker"]}
    if a.swap_sides:
        out["per_speaker_swapped"] = (await run_once(events, gold, llm, swap=True))["per_speaker"]
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"extractor={name} prompt={tag} runs={n} map={runs[0]['map']} gold={len(gold)}")
        for k, v in rows:
            print(f"  {k:<16} {v}")
        print(f"  per speaker     {json.dumps(out['per_speaker'])}")
        if a.swap_sides:
            print(f"  swapped sides   {json.dumps(out['per_speaker_swapped'])}")
    if a.verbose:
        explain(runs[-1], gold)
    if a.tsv:
        path = Path(a.tsv)
        new = not path.exists()
        with path.open("a") as fh:
            if new:
                fh.write("time\tprompt\t" + "\t".join(k for k, _ in rows) + "\n")
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S") + f"\t{tag}\t" + "\t".join(v for _, v in rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
