"""One coloured line per event on stdout. Enabled with PRINT=1 (finals only) or PRINT=2 (+interims).
Also usable on a log file: python -m app.mocks.printer logs/latest.jsonl"""
from __future__ import annotations

import os
import sys
import time

C = {"Utterance": 36, "Pause": 90, "Claim": 33, "Verdict": 32, "Interjection": 35, "DirectorDecision": 90,
     "SourceCard": 34, "ModuleStatus": 31, "SessionStart": 97}
KEYS = {"Utterance": ("speaker", "text"), "Pause": ("speaker",), "Claim": ("id", "speaker", "type", "checkable", "text"),
        "Verdict": ("claim_id", "label", "confidence", "via"), "Interjection": ("kind", "text"),
        "DirectorDecision": ("key", "action", "reason"), "SourceCard": ("claim_id", "url"),
        "ModuleStatus": ("module", "state", "detail"), "SessionStart": ("motion",), "Control": ("flag", "value")}


def fmt(ev) -> str:
    d = ev.model_dump() if hasattr(ev, "model_dump") else ev
    name = d.get("event", "?")
    body = " ".join(f"{k}={d.get(k)!r}" if k != "text" else repr(d.get(k)) for k in KEYS.get(name, ()))
    tag = "" if d.get("final", True) else " (interim)"
    age = time.time() - d.get("ts", time.time())
    return f"\033[{C.get(name, 37)}m{d.get('seq', 0):>5} {d.get('src', ''):<14} {name:<17}\033[0m {body}{tag}  [{age:+.1f}s]"


async def run(bus):
    level = os.environ.get("PRINT", "1")
    async for ev in bus.subscribe("printer", maxsize=2000):
        if ev.event == "Utterance" and not ev.final and level != "2":
            continue
        print(fmt(ev), flush=True)


if __name__ == "__main__":
    from app.bus import load_jsonl
    for e in load_jsonl(sys.argv[1]):
        print(fmt(e))
