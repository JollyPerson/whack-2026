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
WORD_TOLERANCE = 0.015         # "a third" said, 33 or 33.3 written: rounding, not another figure
WORD_AGREEMENT = 0.25          # "about a quarter" for 27% is fair; the fact cards' wrong figures are 70%+ out
SHORT_FACT_WORDS = 10

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_QUOTES = {**dict.fromkeys(map(ord, "‘’‚‛"), "'"), **dict.fromkeys(map(ord, "“”„‟"), '"')}
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(thousand|million|billion|trillion|bn|m|k)?(?![a-z])", re.I)
_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "trillion": 1e12}
_COUNT = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "twenty": 20, "fifty": 50, "hundred": 100}
_PART = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fifth": 5, "fifths": 5,
         "tenth": 10, "tenths": 10}
_C, _P, _S = "|".join(_COUNT), "|".join(_PART), "thousand|million|billion|trillion"
_COUNT_SCALE = re.compile(rf"\b({_C})( and a half)? ({_S})\b")                 # two million, one and a half million
_PART_SCALE = re.compile(rf"\b(?:({_C})[ -])?({_P}) (?:of )?an? ({_S})\b")      # half a million, a quarter of a million
_RATIO = re.compile(rf"\b({_C}|\d+) in (?:every )?({_C}|\d+)\b")                # one in five, 1 in 20
_PART_OF = re.compile(rf"\b(?:({_C})[ -])?({_P})\b")                            # half, a third, three-quarters


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


def word_numbers(text: str) -> list[float]:
    """Figures said in words, which numbers_in can't see. A fraction gives both forms ("three-quarters": 0.75 and
    75), because a percentage in the quote makes the model write it as one."""
    t, out = normalise(text), []
    for n, part, scale in _PART_SCALE.findall(t):
        out.append(_COUNT.get(n, 1) / _PART[part] * _SCALE[scale])
    t = _PART_SCALE.sub(" ", t)                 # read once: "half a million" is neither "a million" nor "half"
    for n, half, scale in _COUNT_SCALE.findall(t):
        out.append((_COUNT[n] + (0.5 if half else 0)) * _SCALE[scale])
    t = _COUNT_SCALE.sub(" ", t)
    for n, d in _RATIO.findall(t):
        top, bottom = (float(w) if w.isdigit() else _COUNT[w] for w in (n, d))
        if bottom:
            out += [top / bottom, 100 * top / bottom]
    for n, part in _PART_OF.findall(t):
        f = _COUNT.get(n, 1) / _PART[part]
        out += [f, 100 * f]
    return out


def has_number(text: str) -> bool:
    return bool(numbers_in(text))


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def number_appears(value: float, text: str) -> bool:
    return any(_close(value, n) for n in numbers_in(text)) or \
        any(_close(value, n, WORD_TOLERANCE) for n in word_numbers(text))


def numbers_agree(claimed: float, actual: float, tol: float = NUMERIC_TOLERANCE) -> bool:
    return _close(claimed, actual, tol)


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
        in_digits = any(_close(numeric.claimed, n) for n in numbers_in(claim_text))
        agree = numbers_agree(numeric.claimed, numeric.actual, NUMERIC_TOLERANCE if in_digits else WORD_AGREEMENT)
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
    if 0 < claimed <= 1 < actual and number_appears(claimed * 100, claim_text):
        claimed *= 100                      # "three-quarters" against a quoted 40%: compare 75 with 40
    if 0 < actual <= 1 < claimed and number_appears(actual * 100, quote):
        actual *= 100
    return Numeric(claimed=claimed, actual=actual, unit=str(raw.get("unit") or ""))
