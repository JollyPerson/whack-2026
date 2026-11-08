"""Verdict cache: the same claim gets the same Verdict, instantly, in every later debate on the Motion. Owner: C.

One JSON-lines file per Motion in the cache volume (cache/verdicts/), so it survives restarts and deploys. A claim
matches a stored one when it's on the same Motion, has exactly the same figures, and shares at least 70% of its
words (the argument map's duplicate threshold). Figures, in digits or words, must match exactly, so "fell 27%"
never reuses the verdict for "fell 30%", nor "a third" for "half". Negations are kept as words, so "did not rise" doesn't match "rose".

Only verdicts worth repeating are stored: a label other than unverifiable, a quote the guard verified, and
confidence of at least EVIDENCE_CACHE_MIN_CONF. A later, better verdict for the same claim (the deep check, say)
replaces the stored one. To forget a Motion's verdicts, delete its file.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.evidence import preresearch
from app.evidence.checks import Guarded, numbers_in, word_numbers
from app.evidence.index import tokens
from app.schemas import Evidence, Numeric

log = logging.getLogger("evidence.cache")

CACHE_DIR = Path(os.environ.get("EVIDENCE_VERDICT_CACHE_DIR", "cache/verdicts"))
MIN_CONFIDENCE = float(os.environ.get("EVIDENCE_CACHE_MIN_CONF", 0.6))
MATCH = 0.7


def words(text: str) -> frozenset[str]:
    return frozenset(t for t in tokens(text) if not any(ch.isdigit() for ch in t))


def figures(text: str) -> tuple[float, ...]:
    """Figures in digits and in words, so "a third of firms" never matches "half of firms"."""
    return tuple(sorted({round(n, 6) for n in numbers_in(text) + word_numbers(text)}))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def worth_keeping(g: Guarded) -> bool:
    return (g.label != "unverifiable" and bool(g.evidence) and not g.quote_rejected
            and g.confidence >= MIN_CONFIDENCE)


@dataclass
class Entry:
    text: str
    words: frozenset[str]
    figures: tuple[float, ...]
    guarded: Guarded
    via: str


def _to_json(text: str, g: Guarded, via: str) -> str:
    return json.dumps({"t": time.time(), "text": text, "via": via, "label": g.label, "confidence": g.confidence,
                       "evidence": [e.model_dump() for e in g.evidence], "explanation": g.explanation,
                       "short_fact": g.short_fact, "numeric": g.numeric.model_dump() if g.numeric else None,
                       "notes": g.notes})


def _from_json(line: str) -> Entry:
    d = json.loads(line)
    g = Guarded(label=d["label"], confidence=float(d["confidence"]), explanation=d.get("explanation", ""),
                evidence=[Evidence(**e) for e in d.get("evidence", [])], short_fact=d.get("short_fact", ""),
                numeric=Numeric(**d["numeric"]) if d.get("numeric") else None, notes=list(d.get("notes", [])))
    return Entry(d["text"], words(d["text"]), figures(d["text"]), g, d.get("via", ""))


class VerdictCache:
    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.entries: list[Entry] = []

    @classmethod
    def for_motion(cls, motion: str, root: Optional[Path] = None) -> "VerdictCache":
        """The Motion's cache, loaded from disk. No Motion means a cache that only lives in memory."""
        if not motion:
            return cls()
        cache = cls(preresearch.index_path(motion, root or CACHE_DIR).with_suffix(".jsonl"))
        if cache.path.exists():
            for line in cache.path.read_text().splitlines():
                try:
                    cache._remember(_from_json(line))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("skipping a bad verdict cache line in %s: %r", cache.path, e)
        return cache

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, text: str) -> Optional[Entry]:
        """The stored verdict for this claim: same figures, and the most words in common above MATCH."""
        w, f = words(text), figures(text)
        best, best_score = None, MATCH
        for entry in self.entries:
            if entry.figures == f:
                score = similarity(w, entry.words)
                if score >= best_score:
                    best, best_score = entry, score
        return best

    def add(self, text: str, g: Guarded, via: str) -> bool:
        """Store a verdict worth repeating. Returns whether it was stored."""
        if not worth_keeping(g):
            return False
        line = _to_json(text, g, via)
        self._remember(_from_json(line))
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(line + "\n")
            except OSError as e:
                log.warning("couldn't save to the verdict cache %s: %r", self.path, e)
        return True

    def _remember(self, entry: Entry) -> None:
        """A newer verdict for the same claim replaces the older one."""
        self.entries = [e for e in self.entries if not (e.figures == entry.figures and e.words == entry.words)]
        self.entries.append(entry)
