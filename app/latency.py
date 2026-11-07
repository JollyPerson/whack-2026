"""Latency meter (design §5). Owner: B.

Every mark is a server wall-clock epoch second stored in `<event>.marks` and copied forward stage by
stage (Utterance.t_end -> Claim.marks -> Verdict.marks), so no joins are needed:
  speech_end (A: Utterance.t_end) · stt_final (bus ts of the final) · claim (B) · retrieved (C)
  · verdict (C, via finish()) · pause / interject (B)

For producers:
  stamp(ev, "retrieved")      record a stage time on an event (model or dict); returns ev
  finish(verdict, claim)      C: call right before publishing a Verdict. Copies claim.marks, stamps
                              "verdict", sets latency_ms = verdict - speech_end. Returns the Verdict.
CLI:
  python -m app.latency [logs/latest.jsonl] [--last] [--json]
Replays at speed != 1 compress every recomputed delta; only Verdict.latency_ms keeps the recorded value,
so run this on the original log.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from app.bus import last_session  # noqa: F401  (re-exported: latency.last_session)
from app.schemas import Event, Verdict

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {   # stage -> target from the latency budget (design §5)
    "stt": "<=1.5s",                       # stt_final - speech_end
    "extract": "<=0.5s",                   # claim - stt_final
    "time_to_chip": "<=2s",                # claim - speech_end   (the pitch metric)
    "retrieve": "<=0.1s idx, <=3s web",    # retrieved - claim
    "judge": "<=2s",                       # verdict - retrieved
    "verdict_e2e": "2-4s idx, 5-7s web",   # Verdict.latency_ms (or verdict - speech_end)
    "interject_after_pause": "<=0.5s",     # interject - pause
}
PAUSE_WINDOW_S = 30.0   # an interjection further than this from the last Pause wasn't released by it


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def stamp(ev: Any, stage: str, t: Optional[float] = None) -> Any:
    """Record `stage` at time t (default now) in ev.marks. Works on any Event (extra field if needed) or dict."""
    t = time.time() if t is None else t
    if isinstance(ev, dict):
        ev["marks"] = {**(ev.get("marks") or {}), stage: t}
    else:
        ev.marks = {**(getattr(ev, "marks", None) or {}), stage: t}   # new dict: never alias a claim's marks
    return ev


def finish(verdict: Union[Verdict, dict], claim: Any = None, now: Optional[float] = None) -> Verdict:
    """C: call right before bus.publish(verdict). `claim` may be the Claim event, its dict, or None."""
    if not isinstance(verdict, Verdict):
        verdict = Verdict.model_validate(verdict)
    t = time.time() if now is None else now
    marks = {**(_get(claim, "marks") or {}), **(verdict.marks or {})}   # C's own stamps win
    end = marks.get("speech_end") or _get(claim, "t_end") or 0.0
    if end > 1e11:                                                      # ms from a JS producer
        end /= 1000.0
    if end:
        marks["speech_end"] = end
        verdict.latency_ms = max(0, round((t - end) * 1000))
    marks["verdict"] = t
    verdict.marks = marks
    return verdict


# ----------------------------------------------------------------------------- analysis (pure)
def pct(xs: list[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (numpy's default); the dashboard header uses the same formula."""
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _stats(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": pct(xs, 0.5), "p90": pct(xs, 0.9), "max": max(xs) if xs else None}


def summarize(events: Iterable[Event]) -> dict:
    """Per-stage latency + counts from events in log (publish) order. First Claim per id and first Verdict
    per claim_id are timed (that is when the chip appears); every Verdict is counted."""
    st: dict[str, list[float]] = {k: [] for k in TARGETS}
    e2e_via: dict[str, list[float]] = {}
    claims: dict[str, Event] = {}
    timed: set[str] = set()
    via, label, kinds = Counter(), Counter(), Counter()
    n_events = sessions = 0
    utt_ts: list[float] = []
    last_pause: Optional[float] = None
    for e in events:
        n_events += 1
        name, m = e.event, getattr(e, "marks", None) or {}
        if name == "SessionStart":
            sessions += 1
        elif name == "Utterance":
            utt_ts.append(e.ts)
            if e.final and e.t_end:
                st["stt"].append(e.ts - e.t_end)
        elif name == "Claim" and e.id not in claims:
            claims[e.id] = e
            c, end = m.get("claim") or e.ts, m.get("speech_end") or e.t_end
            if m.get("stt_final"):
                st["extract"].append(c - m["stt_final"])
            if end:
                st["time_to_chip"].append(c - end)
        elif name == "Verdict":
            via[e.via] += 1
            label[e.label] += 1
            if e.claim_id in timed:
                continue
            timed.add(e.claim_id)
            cl = claims.get(e.claim_id)
            cm = (getattr(cl, "marks", None) or {}) if cl else {}
            vt = m.get("verdict") or e.ts
            end = m.get("speech_end") or cm.get("speech_end") or (cl.t_end if cl else 0)
            d = e.latency_ms / 1000 if e.latency_ms else (vt - end if end else None)
            if d is not None:
                st["verdict_e2e"].append(d)
                e2e_via.setdefault(e.via, []).append(d)
            if m.get("retrieved"):
                claim_t = m.get("claim") or cm.get("claim") or (cl.ts if cl else 0)
                if claim_t:
                    st["retrieve"].append(m["retrieved"] - claim_t)
                st["judge"].append(vt - m["retrieved"])
        elif name == "Pause":
            last_pause = e.t or e.ts
        elif name == "Interjection":
            kinds[e.kind] += 1
            p, t = m.get("pause") or last_pause, m.get("interject") or e.ts
            if p and -1.0 <= t - p <= PAUSE_WINDOW_S:            # small negatives = clock jitter, kept visible
                st["interject_after_pause"].append(t - p)
    span = (max(utt_ts) - min(utt_ts)) / 60 if len(utt_ts) > 1 else 0.0
    return {
        "events": n_events, "sessions": sessions, "duration_min": round(span, 2),
        "claims": len(claims), "checkable": sum(1 for c in claims.values() if c.checkable),
        "claims_per_min": round(len(claims) / span, 1) if span > 0 else None,
        "verdicts": sum(via.values()), "verdicted_claims": len(timed),
        "stages": {k: _stats(v) for k, v in st.items()},
        "verdict_e2e_by_via": {k: _stats(v) for k, v in sorted(e2e_via.items())},
        "verdicts_by_via": dict(via.most_common()), "verdicts_by_label": dict(label.most_common()),
        "interjections_by_kind": dict(kinds.most_common()),
    }


def report(s: dict, title: str = "") -> str:
    def f(v: Optional[float]) -> str:
        return "-" if v is None else f"{v:.2f}s"

    def row(name: str, x: dict, target: str = "") -> str:
        return f"  {name:<24}{x['n']:>5}{f(x['p50']):>9}{f(x['p90']):>9}{f(x['max']):>9}   {target}"

    def counts(c: dict) -> str:
        return "  ".join(f"{k} {v}" for k, v in c.items()) or "-"

    cpm = s["claims_per_min"]
    out = [f"{title}  {s['events']} events  {s['sessions']} session(s)  {s['duration_min']:.1f} min of speech",
           f"claims {s['claims']} ({s['checkable']} checkable, {cpm if cpm is not None else '-'}/min)"
           f"  verdicts {s['verdicts']} on {s['verdicted_claims']} claims",
           f"  {'stage':<24}{'n':>5}{'p50':>9}{'p90':>9}{'max':>9}   target"]
    out += [row(k, v, TARGETS[k]) for k, v in s["stages"].items() if v["n"] or k not in ("retrieve", "judge")]
    out += [row(f"  e2e via {k}", v) for k, v in s["verdict_e2e_by_via"].items()]
    out += [f"verdicts by via:        {counts(s['verdicts_by_via'])}",
            f"verdicts by label:      {counts(s['verdicts_by_label'])}",
            f"interjections by kind:  {counts(s['interjections_by_kind'])}"]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    from app.bus import load_jsonl
    ap = argparse.ArgumentParser(prog="python -m app.latency", description="Per-stage latency from a JSONL log")
    ap.add_argument("path", nargs="?", default=str(ROOT / "logs" / "latest.jsonl"))
    ap.add_argument("--last", action="store_true", help="only the last session (from the last SessionStart)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    a = ap.parse_args(argv)
    if not Path(a.path).exists():
        print(f"no such log: {a.path}", file=sys.stderr)
        return 1
    evs = load_jsonl(Path(a.path))
    s = summarize(last_session(evs) if a.last else evs)
    print(json.dumps(s, indent=1) if a.json else report(s, a.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
