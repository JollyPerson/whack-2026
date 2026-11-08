"""Side-bot wording and guards. Owner: B. Pure: no clock, no I/O, no LLM.

Two bots heckle the opposing debater in speech bubbles: the For bot is Prop's second and answers Opp's claims, the
Against bot is Opp's second and answers Prop's. Every line, templated or written by the LLM, goes through guard():
- at most MAX_WORDS words (a rebut, which names its source, or a uturn, which quotes the debater twice,
  REBUT_MAX_WORDS);
- no judgement or insult words (voice.BANNED and its stems, INSULTS, PUT_DOWNS), no URLs, no emojis or hashtags;
- figures only if the `allowed` text has them (the claim, the Verdict, the line a banter replies to), in digits
  or in words ("two", "half", "forty"); a challenge may carry Claude's own figure because it ends with UNSOURCED
  (a "true, but" challenge only the claim's and its context phrase's); a concede is tagged too, with the claim's
  figures only; a uturn or conceded line (from the debate record: Consistency) only the two claims' figures, and
  no tag, as its quotes are the debater's own words;
- LLM-written lines (strict=True) also may not mention people (PERSONAL) or the debaters' names; banter, which is
  between the two bots, may not mention the debaters at all (DEBATERS) or cite sources.
A line that fails is dropped (or the next template is tried), never trimmed into shape.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from app.brain import voice

PERSONAS = ("for", "against")
ANSWERS = {"opp": "for", "prop": "against"}     # claim side -> the bot that answers it (never that side's own second)
OWN_SIDE = {"for": "prop", "against": "opp"}    # the side each bot seconds
SECOND = {"prop": "for", "opp": "against"}      # side -> its own second (only for nudges about that side's points)
OTHER = {"for": "against", "against": "for"}
PRIORITY = {"rebut": 8, "uturn": 7, "challenge": 6, "concede": 5, "nudge": 4, "conceded": 3, "followup": 2,
            "banter": 1}
FACT_KINDS = ("rebut", "challenge", "concede")  # at most one of these per claim
TAGGED = ("challenge", "concede")               # AI knowledge: the text ends with UNSOURCED
MAX_WORDS = voice.MAX_WORDS
REBUT_MAX_WORDS = 18
LONG = ("rebut", "uturn")                       # a source's name or two quotes: up to REBUT_MAX_WORDS
UNSOURCED = "(unsourced)"
IMPORTANT = 0.5                                 # importance >= this: worth a follow-up or a knowledge check
NO_CHECK = {"value", "prediction"}              # never checkable, whatever the extractor said (as argmap.NO_CHECK)

INSULTS = frozenset({
    "idiot", "idiots", "idiotic", "moron", "morons", "moronic", "dumb", "dimwit", "clown", "clowns", "pathetic",
    "loser", "losers", "ignorant", "clueless", "fool", "fools", "foolish", "silly", "shut", "embarrassing",
    "hypocrite", "hypocrites", "hypocritical", "coward", "cowards", "cowardly", "incompetent", "laughable",
    "dishonest", "shameful", "disgrace", "disgraceful", "garbage", "trash", "crap", "hell", "damn", "bloody",
    "lazy", "weak", "bully", "bullies", "arrogant", "arrogance", "smug", "rude", "desperate", "desperation",
    "panic", "panicking", "panicked", "rattled", "flailing", "floundering", "sloppy", "lame", "childish", "naive",
    "deluded", "delusional", "hysterical", "whining", "whiny", "rambling", "waffling", "bluff", "bluffing",
    "drivel", "hogwash", "baloney", "balderdash", "claptrap", "twaddle", "tosh", "codswallop", "poppycock"})
PUT_DOWNS = re.compile(   # questions about a debater's honesty or wits ("Did you just make that up?")
    r"\b(?:make|made|making) (?:that|this|it|those|these) up\b|\b(?:do|did|does) you even\b|\bthin air\b|"
    r"\bout of (?:your|their) depth\b", re.I)
PERSONAL = frozenset({   # LLM lines never remark on the debaters as people (also drops "he"/"she" gossip)
    "hair", "haircut", "face", "voice", "accent", "outfit", "clothes", "shirt", "tie", "suit", "shoes", "glasses",
    "looks", "ugly", "fat", "skinny", "bald", "sweat", "sweating", "sweaty", "nervous", "stutter", "stammer",
    "mumble", "mumbling", "shouting", "he", "she", "him", "her", "his", "hers", "himself", "herself"})
DEBATERS = frozenset({   # banter is bot to bot: it never mentions the debaters (or anyone else) at all
    "debater", "debaters", "speaker", "speakers", "prop", "proposition", "opp", "opposition", "they", "them",
    "their", "theirs", "themselves", "person", "people", "human", "humans", "champion", "guy", "guys", "man",
    "woman", "folks", "side", "sides", "team", "teams", "opponent", "opponents", "client", "clients"})
SOURCE_WORDS = frozenset({"according", "study", "studies", "survey", "surveys", "poll", "polls", "census",
                          "researchers", "report", "reports"})
NUMBER_WORDS = frozenset({   # not "one": "which one", "no one" (and "one in five" has its "five")
    "zero", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "half", "halves", "quarter",
    "quarters", "third", "thirds", "double", "twice", "triple", "dozen", "dozens",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
    "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "hundreds", "thousand",
    "thousands", "million", "millions", "billion", "billions", "trillion", "percent", "cent", "bn"})
_SOURCE_NAMES = sorted({s.lower() for s in voice.SOURCES.values()}, key=len, reverse=True)
_URL = re.compile(r"\bhttps?://|\bwww\.|\b[\w-]+\.[a-z]{2,}/|"     # a link: a dot then a slash ("bit.ly/x")
                  r"\b[\w-]+\.(?:com|org|net|io|ai|ly|co|me|tv|app|dev|uk|gov|edu|int|eu|info)\b", re.I)
_TAG = re.compile(r"\(?\s*\bunsourced\b\s*\)?\.?", re.I)   # the tag as the LLM may write it: "(Unsourced)."
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[a-z]+")
_LETTERS = re.compile(r"[^\W\d_]+")             # words in any script: a name's "Siobhán", "Anne-Marie", "O'Neill"
_NUM = r"[£$€]?\d[\d,.]*\d?"
_FIGURE = re.compile(rf"{_NUM}(?:\s?(?:-|–|to)\s?{_NUM}|/\d+)?"            # a range ("20-30%", "20 to 30%") or "1/3"
                     r"(?:\s?%|\s?(?:per ?cent|percent|million|billion|trillion|bn|m|k)\b)?", re.I)
_YEAR = re.compile(r"(?:19|20)\d\d")
_DATE = re.compile(r"\b\d{1,4}([/-])\d{1,2}\1\d{1,4}\b|\b(?:19|20)\d\d-(?:0[1-9]|1[0-2])\b")   # 8/11/2026, 2026-11
_ODD = re.compile(r"[#@<>{}\[\]\\|*_~`]")
_QUOTES = "\"'“”‘’`"
_OK_HIGH = set("’‘“”—–…€")


_KEY = re.compile(r"[^a-z0-9%£$€.]+")


def text_key(text: str) -> str:
    """Duplicate test: "Source for that 40%?" and "source for that 40%." are the same line."""
    return _KEY.sub(" ", str(text or "").lower()).strip(" .")


def answering(side: Any) -> Optional[str]:
    """The persona that answers a claim from `side` ("prop" | "opp"); None for anything else."""
    return ANSWERS.get(str(side or "").strip().lower())


def hold_s(text: str) -> float:
    """How long the bubble types and stays up: words / 2.5 + 3 seconds, clamped to 6..12."""
    return min(max(len(text.split()) / 2.5 + 3.0, 6.0), 12.0)


def _get(o: Any, k: str, default: Any = None) -> Any:
    if o is None:
        return default
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def tidy(text: Any) -> str:
    """One line: whitespace collapsed, wrapping quotes stripped."""
    s = " ".join(str(text or "").split()).strip()
    while len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
        s = s[1:-1].strip()
    return s


def _figures(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in _DIGITS.findall(text)}


def new_figures(text: str, allowed: str) -> bool:
    """True if `text` has a figure (in digits, or a number word such as "forty", "million") that `allowed` lacks."""
    if _figures(text) - _figures(allowed):
        return True
    have = set(_WORD.findall(allowed.lower()))
    return any(w in NUMBER_WORDS and w not in have for w in _WORD.findall(text.lower()))


def guard(text: Any, kind: str = "followup", allowed: str = "", strict: bool = False,
          names: Iterable[str] = ()) -> Optional[str]:
    """`text` tidied if it is fit for a bubble, else None. `allowed` holds the figures the line may repeat (ignored
    for a challenge; a challenge or concede must end with UNSOURCED). strict: written by the LLM. `names`: words
    that must not appear (the debaters' names)."""
    s = tidy(text)
    if not s:
        return None
    words = s.split()
    if len(words) > (REBUT_MAX_WORDS if kind in LONG else MAX_WORDS):
        return None
    low = s.lower()
    toks = set(_WORD.findall(low))
    if voice.judgemental(s) or toks & INSULTS or PUT_DOWNS.search(s) or _URL.search(s) or _ODD.search(s):
        return None
    if any(ord(ch) >= 0x2000 and ch not in _OK_HIGH for ch in s):   # emojis and other pictographs
        return None
    if kind in TAGGED and (not s.endswith(UNSOURCED) or s.count(UNSOURCED) != 1):
        return None
    if kind != "challenge" and new_figures(s, allowed):
        return None
    if strict and toks & PERSONAL:
        return None
    if named(low, names):
        return None
    if kind == "banter":
        if toks & SOURCE_WORDS or toks & DEBATERS:
            return None
        allowed_low = allowed.lower()
        if any(re.search(rf"\b{re.escape(n)}\b", low) and n not in allowed_low for n in _SOURCE_NAMES):
            return None
    return s


def named(text: str, names: Iterable[str]) -> bool:
    """True if `text` has one of `names` (the debaters' names), or any part of a hyphenated or apostrophe name,
    accents and all ("Zoë", "Anne-Marie" or "Marie", "O'Neill" or "Neill"). A part needs 3 letters, as a whole name
    does: the "Al" of "Al-Amin" is also "et al."."""
    words = set(_LETTERS.findall(text.lower()))
    return any(p in words for n in names for p in _LETTERS.findall(str(n or "").lower()) if len(p) > 2)


def _finish(text: str) -> str:
    text = " ".join(text.split())
    return text[:1].upper() + text[1:]


def figure(text: str) -> str:
    """The first figure in a claim as said ("40%", "£3 billion", "20-30%", "1/3"), or "". A bare year is not a
    figure, nor a date, nor a "fraction" over 1 ("24/7")."""
    for m in _FIGURE.finditer(_DATE.sub(" ", text or "")):
        f = re.sub(r"\.?\s*%", "%", m.group(0).strip().rstrip(".,"))   # STT's "6 %" and "6.%" are "6%"
        if "/" in f and not _fraction(f):
            continue
        years = all(_YEAR.fullmatch(n) for n in _DIGITS.findall(f)) and \
            not re.search(r"[^\d\s,.–-]", re.sub(r"\bto\b", "", f))
        if any(ch.isdigit() for ch in f) and not years:                  # nor is a span of years ("2010 to 2019")
            return f
    return ""


def _fraction(f: str) -> bool:
    """"1/3" and "2/3" are figures; "24/7" (over 1) and "1/365" (a denominator past 100) are not."""
    try:
        a, b = (float(x.replace(",", "")) for x in re.findall(r"\d[\d,.]*", f)[:2])
    except ValueError:
        return False
    return 0 < a < b <= 100


# ----------------------------------------------------------------------------- sourced rebut
def sourced(verdict: Any) -> bool:
    """A Verdict a bot may quote: contradicted or missing context, with evidence, not capped by C's guards."""
    return (_get(verdict, "label") in ("contradicted", "missing_context") and bool(_get(verdict, "evidence"))
            and "capped" not in (_get(verdict, "guard_notes") or ()))


def supported(verdict: Any) -> bool:
    return _get(verdict, "label") == "supported" and bool(_get(verdict, "evidence"))


def _fig(x: Any, unit: str) -> str:
    """A figure for a bubble: (4.2, "%") -> "4.2%", (3e9, "GBP") -> "£3 billion"."""
    s = voice.say(x, unit)
    return s.replace(" percent", "%") if s else ""


REBUT = {
    "contradicted":    ["{src} says {actual}, not {claimed}. Try again.", "{src} says {fact}. Try again.",
                        "{src} has {fact}, actually.", "{src} says otherwise. Try again."],
    "missing_context": ["{src} says {fact}. Full picture, please.", "Context: {src} says {fact}.",
                        "{src} tells a fuller story. Check it."],
}


def rebut(claim: Any, verdict: Any, used: Iterable[str] = ()) -> Optional[str]:
    """The sourced rebut for a Verdict sourced() accepts, e.g. "The ONS says 4.2%, not 6%. Try again." """
    if not sourced(verdict):
        return None
    num = _get(verdict, "numeric")
    unit = str(_get(num, "unit", "") or "")
    actual, claimed = _fig(_get(num, "actual"), unit), _fig(_get(num, "claimed"), unit)
    if actual == claimed or voice.judgemental(unit):
        actual = claimed = ""
    f = {"src": voice.source_name(_get(verdict, "evidence") or []) or "the source",
         "fact": voice.clean(_get(verdict, "short_fact")), "actual": actual, "claimed": claimed}
    allowed = " ".join([str(_get(claim, "text", "") or ""), str(_get(verdict, "short_fact", "") or ""), actual,
                        claimed, f["src"]] + [f"{_get(e, 'title', '')} {_get(e, 'quote', '')}"
                                              for e in _get(verdict, "evidence") or []])
    used = {text_key(u) for u in used}
    for tpl in REBUT.get(_get(verdict, "label"), []):
        try:
            text = _finish(tpl.format(**{k: v for k, v in f.items() if v}))
        except KeyError:
            continue
        if text_key(text) not in used and (ok := guard(text, "rebut", allowed)):
            return ok
    return None


# ----------------------------------------------------------------------------- AI-knowledge lines
def _body(text: Any) -> str:
    """The LLM's line without any tag it wrote itself ("(Unsourced)."), so the tag is added exactly once."""
    return re.sub(r"\s+([.,;:?!])", r"\1", _TAG.sub(" ", tidy(text))).strip(" ,;:-")


def _tagged(kind: str, tpls: list[str], f: dict, allowed: str, names: Iterable[str], used: Iterable[str] = ()):
    used = {text_key(u) for u in used}
    for tpl in tpls:
        try:
            line = guard(_finish(tpl.format(**{k: v for k, v in f.items() if v})), kind, allowed, True, names)
        except KeyError:
            continue
        if line and text_key(line) not in used:
            return line
    return None


def challenge(claim: Any, text: Any = "", correction: Any = "", names: Iterable[str] = ()) -> Optional[str]:
    """Claude's own-knowledge challenge, tagged: its line if fit, else one built from its correction (a phrase that
    completes "Last I checked, ...", e.g. "it was nearer 4%"). `names`: as for guard()."""
    body = _body(text)
    if body:
        line = guard(f"{_finish(body)} {UNSOURCED}", "challenge", strict=True, names=names)
        if line:
            return line
    fix = voice.clean(correction)
    if not fix:
        return None
    tpls = [f"{{fig}}? Last I checked, {{fix}}. {UNSOURCED}", f"Last I checked, {{fix}}. {UNSOURCED}"]
    return _tagged("challenge", tpls, {"fig": figure(str(_get(claim, "text", "") or "")), "fix": fix}, "", names)


_TRUE_BUT = re.compile(r"^(?:true|granted|sure|yes)?[,.;:]?\s*but\s+", re.I)


def misleading(claim: Any, text: Any = "", context: Any = "", names: Iterable[str] = ()) -> Optional[str]:
    """Claude's "true, but" challenge when the claim's figure stands but is misrepresented (wrong baseline, modelled
    not measured...), tagged: its line if fit, else one built from its context (a phrase that completes "True,
    but ...", e.g. "that's against a modelled baseline"). Figures only from the claim and that context phrase."""
    ctx = _TRUE_BUT.sub("", voice.clean(context))
    claim_text = str(_get(claim, "text", "") or "")
    allowed = f"{claim_text} {ctx}"
    body = _body(text)
    if body and not new_figures(body, allowed):
        line = guard(f"{_finish(body)} {UNSOURCED}", "challenge", strict=True, names=names)
        if line:
            return line
    if not ctx:
        return None
    return _tagged("challenge", [f"{{fig}}? True, but {{ctx}}. {UNSOURCED}", f"True, but {{ctx}}. {UNSOURCED}"],
                   {"fig": figure(claim_text), "ctx": ctx}, "", names)


CONCEDE = ["Annoyingly, that one checks out.", "Fine, that one is actually true.", "Grudgingly: yes, that checks out."]


def concede(claim: Any, text: Any = "", names: Iterable[str] = (), used: Iterable[str] = ()) -> Optional[str]:
    """Claude's grudging concession when a claim is true but widely believed false, tagged: its line if fit, else a
    template. Figures only from the claim."""
    body = _body(text)
    tpls = ([_finish(body).replace("{", "{{").replace("}", "}}")] if body else []) + CONCEDE   # literal braces
    return _tagged("concede", [f"{t} {UNSOURCED}" for t in tpls], {}, str(_get(claim, "text", "") or ""), names, used)


# ----------------------------------------------------------------------------- follow-ups and nudges
FOLLOWUP = {
    "statistic":   ["Source for that {figure}?", "Where does that {figure} come from?", "Source for that number?",
                    "Which year is that figure from?"],
    "comparison":  ["Compared with what, exactly?", "Higher than what, and since when?", "Says who?"],
    "causal":      ["Cause, or just correlation?", "How do you know one caused the other?", "Evidence for the link?"],
    "attribution": ["Where exactly did they say that?", "Quote and date, please.", "Says who, exactly?"],
    "fact":        ["Says who?", "Source, please?", "Can you back that up?"],
    "prediction":  ["What makes you so sure?", "And if it doesn't happen?", "Evidence, or a hunch?"],
    "value":       ["Why should anyone accept that?", "Says who, and why?", "And the case for that is?"],
    "other":       ["Can you back that up?", "Evidence, please?", "Says who?"],
}
GRANTED = ["Granted. So what for the motion?", "Fair enough. Why does it matter here?", "True, and the point is?"]
NUDGE = ["Still waiting on the {topic} point.", "Nothing on {topic}, then?", "Any answer on {topic}?",
         "Still waiting on our last point.", "Our point still stands unanswered."]


def followup(claim: Any, used: Iterable[str] = (), granted: bool = False) -> Optional[str]:
    """A template follow-up question by claim type (the fallback when the LLM fails or is absent)."""
    text = str(_get(claim, "text", "") or "")
    f = {"figure": figure(text)}
    used = {text_key(u) for u in used}
    tpls = GRANTED if granted else FOLLOWUP.get(_get(claim, "type"), FOLLOWUP["other"])
    for tpl in tpls:
        try:
            line = _finish(tpl.format(**{k: v for k, v in f.items() if v}))
        except KeyError:
            continue
        if text_key(line) not in used and (ok := guard(line, "followup", text)):
            return ok
    return None


def nudge(topic: Any = "", text: str = "", used: Iterable[str] = ()) -> Optional[str]:
    """'Still waiting on the cost point.' `topic` is our own claim's topic; `text` its claim text (its figures)."""
    t = _ARTICLE.sub("", voice.clean(topic))       # "the cost" -> "the cost point", not "the the cost point"
    t = " ".join(t.split()[:4]) if t else ""
    used = {text_key(u) for u in used}
    for tpl in NUDGE:
        try:
            line = _finish(tpl.format(**({"topic": t} if t else {})))
        except KeyError:
            continue
        if text_key(line) not in used and (ok := guard(line, "nudge", f"{t} {text}")):
            return ok
    return None


# ----------------------------------------------------------------------------- the debate record (Consistency)
UTURN = ["Earlier: '{then}'. Now: '{now}'. Which is it?", "You said '{then}'. Now it's '{now}'?",
         "That's not what you said earlier. Which is it?"]
CONCEDED = ["Noted: you just conceded the {topic} point.", "So you grant the {topic} point. Noted.",
            "Noted: you just conceded our point.", "So you grant our point. Noted."]
QUOTE_WORDS = REBUT_MAX_WORDS - 5               # what UTURN's quoted templates leave for the two quotes
_QUOTE_ENDS = " .,;:!?\"'“”‘’"
_ARTICLE = re.compile(r"^(?:the|a|an|our|your)\s+", re.I)   # "the cost" -> "the cost point", not "the the"


def excerpt(text: Any, n: int) -> str:
    """At most `n` words of a quote, verbatim, with an ellipsis where it was cut; a cut keeps the quote's first
    figure in view ("…would cut it by 6% within…")."""
    words = tidy(text).strip(_QUOTE_ENDS).split()
    if n <= 0 or not words:
        return ""
    if len(words) <= n:
        return " ".join(words)
    at = next((i for i, w in enumerate(words) if any(ch.isdigit() for ch in w)), 0)
    start = max(min(at - n // 2, len(words) - n), 0) if at >= n else 0
    s = " ".join(words[start:start + n]).strip(_QUOTE_ENDS)
    return ("…" if start else "") + s + ("…" if start + n < len(words) else "")


def _pick(kind: str, tpls: list[str], f: dict, allowed: str, names: Iterable[str], used: Iterable[str]):
    """The first template that fills (every field it names is non-empty), is new and passes guard()."""
    used = {text_key(u) for u in used}
    for tpl in tpls:
        try:
            line = _finish(tpl.format(**{k: v for k, v in f.items() if v}))
        except KeyError:
            continue
        if text_key(line) not in used and (ok := guard(line, kind, allowed, names=names)):
            return ok
    return None


def uturn(then: Any, now: Any, names: Iterable[str] = (), used: Iterable[str] = ()) -> Optional[str]:
    """The heckle for a self-contradiction, quoting the debater's two claims verbatim (Consistency.then and .now,
    code-checked against the claims upstream), each trimmed to fit: "Earlier: 'cut it by 6%'. Now: 'about 4%'.
    Which is it?" The quotes' figures are the debater's own, so allowed; no tag. Quotes that fail the guards (a
    judgement word, a debater's name) give way to a line without them."""
    t, w = tidy(then).split(), tidy(now).split()
    first = min(len(t), max(QUOTE_WORDS - len(w), QUOTE_WORDS // 2))     # then gets what now leaves, at least half
    f = {"then": excerpt(then, first), "now": excerpt(now, QUOTE_WORDS - first)}
    return _pick("uturn", UTURN, f, f"{tidy(then)} {tidy(now)}", names, used)


def conceded(topic: Any = "", text: str = "", names: Iterable[str] = (), used: Iterable[str] = ()) -> Optional[str]:
    """'Noted: you just conceded the cost point.' `topic`: the conceded point's; `text`: the two claims' words (the
    only figures the line may carry)."""
    t = _ARTICLE.sub("", voice.clean(topic))
    return _pick("conceded", CONCEDED, {"topic": " ".join(t.split()[:4]) if t else ""}, text, names, used)
