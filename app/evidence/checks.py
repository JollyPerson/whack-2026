"""Guards that code applies to every LLM verdict. Owner: C. No I/O, no LLM.

A quote must really exist in the retrieved source, numbers are compared here rather than by the model, and
confidence only reaches the Director's speaking threshold (0.75) when the evidence earns it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from app.schemas import Evidence, Numeric

MIN_QUOTE_CHARS = 12
CONFIDENCE_CAP = 0.74          # just under config.DIRECTOR_MIN_CONF: shown on the dashboard, never spoken
NUMERIC_TOLERANCE = 0.05       # a claimed figure within 5% of the source agrees with it
SHORT_FACT_WORDS = 10

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_QUOTES = {**dict.fromkeys(map(ord, "‘’‚‛"), "'"), **dict.fromkeys(map(ord, "“”„‟"), '"')}
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(thousand|million|billion|trillion|bn|m|k)?(?![a-z])", re.I)
_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}


def normalise(text: str) -> str:
    """Forgive typography and whitespace only, so a passing quote is still the source's own words."""
    text = unicodedata.normalize("NFKC", text or "").translate(_DASHES).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in_source(quote: str, source: str) -> bool:
    q = normalise(quote)
    return len(q) >= MIN_QUOTE_CHARS and q in normalise(source)


def numbers_in(text: str) -> list[float]:
    out = []
    for whole, frac, scale in _NUMBER.findall(normalise(text)):
        value = float(whole.replace(",", "") + frac)
        out.append(value * _SCALE.get(scale.lower(), 1.0))
    return out


def has_number(text: str) -> bool:
    return bool(numbers_in(text))


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def number_appears(value: float, text: str) -> bool:
    return any(_close(value, n) for n in numbers_in(text))


def numbers_agree(claimed: float, actual: float) -> bool:
    return _close(claimed, actual, NUMERIC_TOLERANCE)


@dataclass
class Source:
    url: str
    title: str
    text: str


@dataclass
class Guarded:
    label: str = "unverifiable"
    confidence: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)
    explanation: str = ""
    short_fact: str = ""
    numeric: Optional[Numeric] = None
    notes: list[str] = field(default_factory=list)   # quote_rejected, numeric_override, numeric_dropped, capped

    @property
    def quote_rejected(self) -> bool:
        return "quote_rejected" in self.notes


def _unverifiable(explanation: str, *notes: str) -> Guarded:
    return Guarded(explanation=explanation, notes=list(notes))


def apply_guards(raw: dict, claim_text: str, sources: list[Source]) -> Guarded:
    """Turn the model's answer into what we're willing to publish. When unsure, the verdict is unverifiable."""
    label = raw.get("label")
    if label not in ("supported", "contradicted", "missing_context"):
        return _unverifiable(str(raw.get("explanation") or "No source settles this."))

    idx, quote = raw.get("source"), str(raw.get("quote") or "")
    source = sources[idx] if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(sources) else None
    if source is None or not quote_in_source(quote, source.text):
        return _unverifiable("The model's quote wasn't found in the source, so the verdict was dropped.", "quote_rejected")

    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    notes: list[str] = []
    capped = not raw.get("directly_states")

    numeric = _checked_numeric(raw, claim_text, quote)
    if numeric is not None:
        agree = numbers_agree(numeric.claimed, numeric.actual)
        if label != "missing_context" and agree != (label == "supported"):
            label = "supported" if agree else "contradicted"
            notes.append("numeric_override")
            capped = True
    elif raw.get("claimed") is not None or raw.get("actual") is not None:
        notes.append("numeric_dropped")
        capped = True
    elif has_number(claim_text) and label != "missing_context":
        capped = True                       # a figure nobody compared in code isn't worth saying aloud

    if capped and confidence > CONFIDENCE_CAP:
        confidence = CONFIDENCE_CAP
        notes.append("capped")
    short_fact = " ".join(str(raw.get("short_fact") or "").split()[:SHORT_FACT_WORDS])
    return Guarded(label=label, confidence=confidence, numeric=numeric, notes=notes, short_fact=short_fact,
                   explanation=str(raw.get("explanation") or ""),
                   evidence=[Evidence(url=source.url, title=source.title, quote=quote)])


def _checked_numeric(raw: dict, claim_text: str, quote: str) -> Optional[Numeric]:
    claimed, actual = raw.get("claimed"), raw.get("actual")
    if not isinstance(claimed, (int, float)) or not isinstance(actual, (int, float)):
        return None
    if isinstance(claimed, bool) or isinstance(actual, bool):
        return None
    if not number_appears(claimed, claim_text) or not number_appears(actual, quote):
        return None                         # the model's numbers must be the ones actually said and quoted
    return Numeric(claimed=claimed, actual=actual, unit=str(raw.get("unit") or ""))
