"""Interjection wording. Owner: B. Template-first: deterministic, <= 15 words (~5 s of TTS), names the source.

Pure and independent of the Director, so D can preview lines or pre-synthesise TTS with the same code:
    render("correction", claim=claim, verdict=verdict, side="Proposition", age_s=4.0)
claim/verdict may be schemas objects, plain dicts or None; every field is optional.

- Templates are tried in order; the first whose fields all exist and that fits MAX_WORDS wins.
- "Earlier, on {topic}" templates are only tried once the claim is older than EARLIER_AFTER_S.
- The last template of every list needs at most {src}/{side}, which have fallbacks, so it always renders.
- Dry referee register: no judgement words. A dynamic field (e.g. C's free-text short_fact) containing a
  BANNED word or a word starting with a BANNED_STEMS stem counts as missing, so the next template is used.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable
from urllib.parse import urlparse

MAX_WORDS = 15
EARLIER_AFTER_S = 15.0
SIDE_LABEL = {"prop": "Proposition", "opp": "Opposition"}      # spoken names; never personal names by default
FALLBACK = {"src": "the source", "side": "Speaker"}
OPENER = "I'm the referee. I'll flag checkable claims; sources on screen."

SOURCES = {  # evidence domain -> spoken source name (longest match wins, so ons.gov.uk beats gov.uk)
    "ons.gov.uk": "the ONS", "obr.uk": "the OBR", "ifs.org.uk": "the IFS", "fullfact.org": "Full Fact",
    "nhs.uk": "NHS England", "bbc.co.uk": "the BBC", "bbc.com": "the BBC", "parliament.uk": "Parliament",
    "gov.uk": "GOV.UK", "tfl.gov.uk": "TfL", "london.gov.uk": "City Hall", "who.int": "the WHO",
    "worldbank.org": "the World Bank", "oecd.org": "the OECD", "imf.org": "the IMF", "un.org": "the UN",
    "europa.eu": "the EU", "wikipedia.org": "Wikipedia", "ourworldindata.org": "Our World in Data",
    "reuters.com": "Reuters", "apnews.com": "AP", "theguardian.com": "the Guardian", "ft.com": "the FT",
    "economist.com": "the Economist", "nature.com": "Nature", "statista.com": "Statista",
    "census.gov": "the Census Bureau", "bls.gov": "the BLS", "cdc.gov": "the CDC",
}
_DOMAINS = sorted(SOURCES, key=len, reverse=True)
CURRENCY = {"GBP": "£", "£": "£", "USD": "$", "$": "$", "EUR": "€", "€": "€"}

T = {
    "correction": ["Earlier, on {topic}: {src} puts it at {actual}, not {claimed}.",
                   "Earlier, on {topic}: {src} says {short_fact}.",
                   "Earlier, on {topic}: {src} contradicts that. Details on screen.",
                   "Point of fact: {src} puts that at {actual}, not {claimed}.",
                   "Point of fact: {src} says {short_fact}.",
                   "Point of fact: {src} contradicts that. Details on screen."],
    "context":    ["Earlier, on {topic}: some context, {src} notes {short_fact}.",
                   "Earlier, on {topic}: that needs context from {src}, on screen.",
                   "Some context: {src} notes {short_fact}.",
                   "That needs context: {src} has the fuller picture, on screen."],
    "confirm":    ["Earlier, on {topic}: that checks out. Source: {src}.",
                   "For the record, that checks out. Source: {src}."],
    "source_req": ["{side}, what's your source on {topic}? I couldn't verify it.",
                   "{side}, what's your source for that? I couldn't verify it."],
    "prompt":     ["{side}, you haven't yet answered the point on {topic}.",
                   "{side}, your opponent's last point is still unanswered."],
    "time":       ["Time, {side}. Please wrap up."],
    "generic":    ["Referee's note: details are on screen."],
}
BANNED = frozenset({
    "lie", "lies", "lied", "lying", "liar", "liars", "wrong", "false", "falsely", "untrue", "incorrect",
    "misleading", "mislead", "misled", "dishonest", "dishonestly", "nonsense", "rubbish", "fake", "bogus",
    "ridiculous", "absurd", "stupid", "debunked", "fabricated", "deceptive", "deceitful"})
BANNED_STEMS = ("exaggerat", "inaccura", "fabricat", "mislead", "distort", "untruth", "falsehood", "falsif", "myth",
                "mistaken", "baseless", "unfounded", "erroneous", "misinform", "disinform", "bogus", "spurious",
                "debunk", "decepti", "deceit", "dishonest")
_WORD = re.compile(r"[a-z]+")


def judgemental(text: str) -> bool:
    ws = _WORD.findall(str(text).lower())
    return bool(BANNED & set(ws)) or any(w.startswith(BANNED_STEMS) for w in ws)


def _get(o: Any, k: str, default: Any = None) -> Any:
    if o is None:
        return default
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def clean(text: Any) -> str:
    """Whitespace-normalised field text, or "" if empty or it carries a judgement word."""
    s = " ".join(str(text or "").split()).strip(" .;:,")
    return "" if not s or judgemental(s) else s


def ok(text: str) -> bool:
    """A line fit to speak: non-empty, <= MAX_WORDS, no judgement words (use it to vet any LLM rephrase)."""
    return bool(text.strip()) and len(text.split()) <= MAX_WORDS and not judgemental(text)


def _site_from_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    for sep in (" | ", " - ", " — ", " – "):        # "Labour market overview - Office for National Statistics"
        if sep in t:
            site = t.rsplit(sep, 1)[1].strip()
            if 0 < len(site.split()) <= 4:
                return site
    return " ".join(t.split()[:3]).rstrip(":;,.-")


def source_name(evidence: Iterable[Any]) -> str:
    """Spoken name for the first usable evidence item: known domain > site name in title > title > host."""
    for e in evidence or []:
        host = (urlparse(str(_get(e, "url", "") or "")).hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        for dom in _DOMAINS:
            if host == dom or host.endswith("." + dom):
                return SOURCES[dom]
        name = clean(_site_from_title(_get(e, "title", ""))) or clean(host)
        if name:
            return name
    return ""


def _num(x: float) -> str:
    for div, word in ((1e12, " trillion"), (1e9, " billion"), (1e6, " million")):
        if x >= div:
            return f"{round(x / div, 2):g}{word}"
    return f"{int(round(x)):,}" if x >= 1000 or float(x).is_integer() else f"{round(x, 2):g}"


def say(x: Any, unit: str = "") -> str:
    """Speakable number: (4.1, "%") -> "4.1 percent", 7.6e6 -> "7.6 million", (3e9, "GBP") -> "£3 billion"."""
    if x is None or isinstance(x, bool):
        return ""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(x):
        return ""
    u, sign = str(unit or "").strip(), "-" if x < 0 else ""
    if u.lower() in ("%", "percent", "per cent", "pct"):
        return f"{sign}{_num(abs(x))} percent"
    pre = CURRENCY.get(u.upper(), "")
    return f"{sign}{pre}{_num(abs(x))}" + ("" if pre or not u else f" {u}")


def fields(claim: Any = None, verdict: Any = None, side: str = "") -> dict:
    """Template fields that exist (missing ones are left out, so templates needing them are skipped)."""
    num = _get(verdict, "numeric")
    unit = _get(num, "unit", "") or ""
    actual, claimed = say(_get(num, "actual"), unit), say(_get(num, "claimed"), unit)
    if actual == claimed:                         # nothing to contrast
        actual = claimed = ""
    f = {"src": source_name(_get(verdict, "evidence") or []), "short_fact": clean(_get(verdict, "short_fact")),
         "topic": clean(_get(claim, "topic")), "side": " ".join(clean(side).split()[:3]),
         "actual": actual, "claimed": claimed}
    return {k: v for k, v in f.items() if v}


def _finish(text: str) -> str:
    words = text.split()[:MAX_WORDS]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def render(kind: str, claim: Any = None, verdict: Any = None, side: str = "", age_s: float = 0.0) -> str:
    """The line the referee says for a `kind` candidate. `side` = spoken name of the speaker addressed."""
    f = fields(claim, verdict, side)
    tpls = T.get(kind) or T["generic"]
    for tpl in tpls[:-1]:
        if tpl.startswith("Earlier") and age_s <= EARLIER_AFTER_S:
            continue
        try:
            text = tpl.format(**f)
        except KeyError:
            continue
        if len(text.split()) <= MAX_WORDS:
            return _finish(text)
    return _finish(tpls[-1].format(**{**FALLBACK, **f}))
