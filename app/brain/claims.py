"""Claim extraction. Owner: B.

Call order (driven by brain/run.py):
  units = ex.assemble(u, now)      # final Utterance -> 0..n sentence-complete units (buffers fragments);
                                   # an interim only keeps that speaker's fragment open
  units = ex.on_pause(speaker)     # starts that speaker's short Pause grace (returns only overdue fragments)
  units = ex.flush_due(now)        # flush fragments quiet long enough (FRAG_* timers) or too old (~4 Hz)
  units = ex.flush()               # end of input: flush everything
  claims = await ex.extract(unit)  # LLM (or heuristic fallback) -> publishes Claim events, returns them

Per sentence: LLM (structured JSON, CLAIM_TIMEOUT_S cap, semaphore) unless there is no LLM or the speech is
stale; any failure falls back to the deterministic heuristic below and is counted in stats["by_cause"]. The LLM
decides text/type/checkable/importance/topic/links; code decides ids, side, dedupe, link filtering and the
value/prediction => uncheckable rule. argmap.add_claim() always runs before publish().
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from app import config
from app.brain.argmap import ArgMap
from app.brain.llm import LLMError, LLMInvalid, LLMTimeout, parse_json_text
from app.brain.prompts import CLAIM_TYPES, OUTPUT_SCHEMA, SYSTEM_PROMPT, render_user
from app.schemas import Claim, Event, Link, Utterance

log = logging.getLogger("brain.claims")

_cid = itertools.count(1)          # process-global: claim ids never repeat across sessions
IMPORTANCE = {1: 0.33, 2: 0.67, 3: 1.0}
NO_CHECK = {"value", "prediction"}
KNOWN_TYPES = set(CLAIM_TYPES) | {"other"}
ELIGIBLE = 0.5                     # argument-map "eligible point" threshold
MAX_PER_SENTENCE = 3
CONTEXT_LIMIT = 15
# Fragment timers (s, our clock). QUIET: since the speaker's last sign of speech (a final, or an interim that
# moved on). PAUSE: since a Pause, if no speech followed. Interims prove the speaker is still talking, so with
# them a short silence is real; finals-only ears show no sign of life between finals (2-3 s apart in continuous
# speech), so wait longer. A Pause never flushes at once: VADs also fire at breaths mid-sentence, and a
# half-sentence flushed early can never be re-joined (cost: <= PAUSE extra latency on an unpunctuated tail).
FRAG_QUIET_S, FRAG_QUIET_FINALS_S = 2.0, 3.0
FRAG_PAUSE_S, FRAG_PAUSE_FINALS_S = 1.0, 2.0
FRAG_MAX_AGE_S, FRAG_MAX_WORDS = 8.0, 40      # hard caps: nothing is held longer / bigger than this
INTERIM_LIVE_S = 5.0                          # an interim this recent from the speaker -> the shorter timers
CLOCK_SKEW_S = 30.0                           # |t_end - ts| beyond this: t_end is on another clock, use ts
MIN_CALL_S = 0.3                              # no LLM call with less left than this (or half CLAIM_TIMEOUT_S)
LLM_MAX_TOKENS = 400
WARMUP_TIMEOUT_S = 15.0            # first call compiles the schema; give it room
WARMUP_SENTENCE = "Unemployment has risen by two percent since last year, according to the ONS."
CAUSES = ("timeout", "error", "refusal", "invalid", "stale", "no_llm")


def next_claim_id() -> str:
    return f"c{next(_cid)}"


@dataclass
class Draft:
    """One claim before ids/side/marks are attached (LLM and heuristic both produce these)."""
    text: str
    type: str
    checkable: bool
    importance: float
    topic: str = ""
    links: list = field(default_factory=list)      # [(claim_id, "supports" | "attacks")]


@dataclass
class _Frag:
    first: Utterance
    last: Utterance
    text: str
    t0: float                           # when this fragment started buffering
    t_final: float                      # when `last` arrived (the stale check doesn't count our hold)
    t_seen: float                       # last sign of speech from this speaker
    paused_at: Optional[float] = None   # first Pause since t_seen


# ============================================================================ sentence handling
_TERM_END = re.compile(r"[.!?][\"'”’)\]]*\s*$")
_TERM = re.compile(r"[.!?][\"'”’)\]]*(?=\s|$)")
_SPLIT = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'”’)\]])\s+")
_ABBR = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|St|vs|approx|e\.g|i\.e)\.)$", re.I)
_LONG_CUT = re.compile(r",\s|\s(?:and|but|because|so|which|while|whereas)\s")
_NORM = re.compile(r"[^\w\s']+")
_FILLER = re.compile(
    r"(?:(?:and|so|well|okay|ok|right|yes|no|sorry|look|listen|um+|uh+|er+|erm)\s+)*(?:"
    r"thanks?(?: you)?(?: (?:very|so) much)?(?: (?:chair|madam chair|mr speaker|everyone|all))?"
    r"|(?:can|could|may) i (?:just )?(?:finish|continue|carry on|make my point|get a word in)"
    r"(?: (?:my|this) point)?(?: please)?"
    r"|(?:please )?let me (?:just )?(?:finish|continue|speak|answer|make my point)(?: please)?"
    r"|i(?:'ll| will) (?:take|accept) (?:that|the|your) (?:point|intervention|question)"
    r"|(?:on a )?point of (?:information|order)|order(?: order)*|hear hear|go on|carry on|excuse me|if i may"
    r"|with (?:all due )?respect|no thank you|sit down|let me move on|moving on|next point|that's my point"
    r"|i'll come back to that|over to you|your turn|ok(?:ay)?|yes|no|right|sorry"
    r")(?: (?:please|then|now|thanks|thank you))*")


def _norm(s: str) -> str:
    return " ".join(_NORM.sub(" ", s.lower()).split())


def _split_long(s: str, limit: int = 40) -> list[str]:
    """STT punctuation can be weak: cut a very long 'sentence' at the comma/conjunction nearest its middle."""
    if len(s.split()) <= limit:
        return [s]
    cands = [m.start() for m in _LONG_CUT.finditer(s)]
    if not cands:
        return [s]
    cut = min(cands, key=lambda i: abs(i - len(s) // 2))
    left, right = s[:cut].strip(" ,"), s[cut:].strip(" ,")
    if not left or not right:
        return [s]
    return _split_long(left, limit) + _split_long(right, limit)


def split_sentences(text: str) -> list[str]:
    """Sentence split on terminal punctuation (abbreviation-aware), long sentences cut near the middle."""
    out: list[str] = []
    for piece in _SPLIT.split((text or "").strip()):
        piece = piece.strip()
        if not piece:
            continue
        if out and _ABBR.search(out[-1]):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return [p for s in out for p in _split_long(s)]


def keep_sentence(s: str) -> bool:
    """Drop filler ("thank you", "can I finish my point") and fragments under 4 words (unless numeric)."""
    n = _norm(s)
    if not n or _FILLER.fullmatch(n):
        return False
    return len(n.split()) >= 4 or has_quantity(s)


def _split_complete(text: str) -> tuple[str, str]:
    """(complete sentences, trailing fragment without terminal punctuation)."""
    ends = [m.end() for m in _TERM.finditer(text)]
    if not ends:
        return "", text.strip()
    return text[:ends[-1]].strip(), text[ends[-1]:].strip()


# ============================================================================ heuristic extractor
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’\-]*|\d+(?:,\d{3})*(?:\.\d+)?")
_POSS = re.compile(r"['’]s$")
_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_YEAR = re.compile(r"(?:1[89]|20)\d\d")
_NUMWORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|"
            r"fifty|sixty|seventy|eighty|ninety|a hundred|hundred|a thousand)")
QTY = re.compile(
    r"[£$€]\s?\d"
    r"|\d[\d,.]*\s*(?:%|percent\b|per cent\b|pc\b|k\b|m\b|bn\b|million\b|billion\b|trillion\b|thousand\b|hundred\b)"
    r"|\b(?:half|third|thirds|quarter|quarters|fifth|tenth|twice|doubled|tripled|trebled|halved|quadrupled)\b"
    r"|\b" + _NUMWORD + r"\s+(?:percent|per cent|million|billion|thousand|hundred|times|in (?:two|three|four|five|"
    r"ten|a hundred))\b"
    r"|\b(?:millions|billions|thousands|hundreds) of\b", re.I)
VALUE = re.compile(
    r"\b(?:should|shouldn't|ought to|must|mustn't|need to|needs to|have got to|has got to|right thing|wrong thing|"
    r"morally|immoral|unfair|fair|unjust|deserve|deserves|it's time to|it is time to|the answer is|"
    r"the solution is|the way forward|we need|let's|better off|worth it|a shame|disgrace|outrageous|ridiculous|"
    r"absurd|free to choose|too \w+ to)\b", re.I)
PRED = re.compile(
    r"\b(?:will|won't|shall|going to|gonna|would|wouldn't|is set to|are set to|bound to|in the future|next year|"
    r"next decade|within (?:the next )?(?:\w+ )?(?:years?|months?|decades?)|by (?:the year )?20\d\d|"
    r"in (?:\w+ )?years' time|soon)\b", re.I)
COMPARE = re.compile(
    r"\b(?:more|less|fewer|higher|lower|bigger|smaller|larger|better|worse|cheaper|dearer|faster|slower|greater|"
    r"safer|longer|shorter|richer|poorer)\b[^.?!]{0,60}?\bthan\b"
    r"|\b(?:ris(?:en|es|ing)|rose|fall(?:en|s|ing)|fell|increas(?:ed|es|ing)|decreas(?:ed|es|ing)|"
    r"declin(?:ed|es|ing)|grow(?:n|s|ing)|grew|dropp(?:ed|ing)|doubled|halved|tripled|soared|plummeted|surged|"
    r"slumped|went (?:up|down)|gone (?:up|down)|going (?:up|down)|cut|cuts|reduced|reduces|reducing|"
    r"getting (?:better|worse)|record (?:high|low))\b"
    r"|\b(?:the|its|their|our|single) (?:biggest|largest|smallest|highest|lowest|best|worst|fastest|slowest|"
    r"greatest|most|least)\b", re.I)
CAUSAL = re.compile(
    r"\b(?:because|caus(?:e|es|ed|ing)|leads? to|led to|leading to|results? in|resulted in|resulting in|due to|"
    r"thanks to|responsible for|contribut(?:es|ed|ing) to|driv(?:es|en|ing)|prevents?|prevented|stops?|stopped|"
    r"blamed?)\b", re.I)
VAGUE = re.compile(r"\b(?:things|everything|everyone|everybody|nobody|no one|anyone|so many|so much|lots of|"
                   r"a lot of|loads of|tons of|all the time|everywhere|nowadays|these days)\b", re.I)
MEASURE = re.compile(r"\bper (?:day|week|month|year|annum|head|person|capita|household|hour)\b|\ba (?:day|week|"
                     r"month|year)\b|\b(?:rate|average|median|proportion|share|majority|minority)\b", re.I)
METRIC = re.compile(
    r"\b(?:deaths?|died|emissions?|pollution|nitrogen|dioxide|carbon|crimes?|unemployment|employment|jobs?|"
    r"inflation|prices?|wages?|salar(?:y|ies)|incomes?|earnings|gdp|growth|economy|poverty|homeless\w*|debt|"
    r"deficit|tax(?:es)?|spending|spend|spent|revenue|sales|footfall|traffic|congestion|accidents?|injur\w+|"
    r"casualties|cancer|asthma|disease|hospitals?|patients?|turnout|population|births?|immigra\w+|migra\w+|"
    r"exports?|imports?|productivity|ridership|journeys|passengers|fares?|budget|funding|costs?)\b", re.I)
REBUT_START = re.compile(
    r"^(?:but|no|nope|actually|wrong|false|nonsense|rubbish|not true|in fact|on the contrary|however|"
    r"that(?:'s| is) (?:not|wrong|false|simply not|just not|untrue|misleading|a myth|nonsense|rubbish)|"
    r"that (?:[\w,]+ ){0,2}(?:figure|number|statistic|stat|claim|study|report|estimate|point|argument|idea|data|"
    r"survey|poll)s?\b|the (?:\w+ ){0,3}(?:is|are) wrong)", re.I)
REBUT_ANY = re.compile(
    r"\b(?:not true|isn't true|is wrong|are wrong|was wrong|misleading|a myth|irrelevant|nonsense|ignores|ignoring|"
    r"doesn't hold|is false|isn't the case|not the case|debunked|cherry-?pick\w*|exaggerat\w*|out of date|"
    r"outdated|doesn't mean|does not mean|no evidence)\b", re.I)
CONTRAST = re.compile(r"\b(?:actually|in fact|not|isn't|aren't|wasn't|don't|doesn't|won't|rather|instead|"
                      r"opposite)\b", re.I)
_CLAUSE = re.compile(r",\s+(and|but|while|whereas|because|so)\s+", re.I)
_LEAD_PUNCT = re.compile(r"^(?:no|yes|right|well|now|look|listen|okay|ok|so|again|also|indeed|clearly|obviously|"
                         r"first(?:ly)?|second(?:ly)?|third(?:ly)?|finally|lastly|sorry|thanks|chair|madam chair|mr speaker|"
                         r"thank you(?: very much)?(?:,? (?:chair|madam chair|mr speaker|everyone))?)\s*[,.:;!]+\s*",
                         re.I)
_RHETORICAL = re.compile(r"^(?:isn't it (?:true|the case) that|is it not (?:true|the case) that|did you know that|"
                         r"don't you think that|wouldn't you agree that|do you deny that)\s+", re.I)
_LEAD_FREE = re.compile(r"^(?:and|but|so|honestly|frankly|basically|actually|of course|you know|i mean|in fact|"
                        r"in conclusion|to be clear|let me be clear|let's be clear|the (?:fact|truth|reality|point) "
                        r"is(?: that)?|the thing is|we know(?: that)?|it(?:'s| is) clear(?: that)?)\b[,:]?\s+", re.I)
_HEDGE = re.compile(r"^(?:i|we)(?:'d|'m)?\s+(?:(?:would|just|really|honestly|genuinely|do|still)\s+)*"
                    r"(?:think|believe|feel|reckon|argue|say|suggest|contend|maintain)\b(?:\s+that)?[,\s]*"
                    r"|^(?:in my (?:view|opinion)|personally|for me|my view is(?: that)?|"
                    r"it seems to me(?: that)?)[,\s]+", re.I)
_INLINE = re.compile(r"\s*\b(?:actually|simply|basically|literally|honestly|obviously|frankly|"
                     r"just(?!\s+(?:\d|over\b|under\b|about\b|a (?:third|quarter|half|fifth|tenth|few)\b|half\b|"
                     + _NUMWORD + r"\b)))\b,?", re.I)

STOP = set("""a an the and or but so of to in on at by for from with without into onto over under about above
below than then that this these those there here it its it's is are was were be been being am do does did done
has have had having will would shall should can could may might must not no nor as if when while where which who
whom whose what why how i me my we us our you your he him his she her they them their one ones some any all each
every both either neither other another such same very much many more most less least few own just also too
only even still yet up down out off again once around nearly almost per whole according let's that's there's
since ever against until after before during though although unless whether because within
exactly really quite rather simply clearly obviously basically actually literally honestly frankly certainly
definitely absolutely totally completely entirely""".split())
_AUX = set("""is are was were has have had will would can could should must may might does did do isn't aren't
wasn't weren't hasn't haven't won't wouldn't can't couldn't doesn't didn't don't""".split())
_VERBS = set("""cause causes make makes made spend spends rely relies cut cuts rose fell went goes go say says said
show shows mean means need needs want wants pay pays cost costs kill kills arrive arrives think know see
choose invest keep replace give gives get gets take takes""".split())
_GENERIC = set("year years people time thing things way lot number figure point argument fact country today "
               "week month day make say get think".split())
_UNITS = {"percent", "per", "cent", "million", "billion", "thousand", "hundred", "pounds", "%"}
_ORGS = set("""government govt council councils ministry minister ministers department treasury parliament commons
lords commission committee office police nhs bank banks university universities institute regulator ofsted ofcom
ofgem tfl defra court inquiry watchdog chancellor mayor authority agency foundation census labour tories
conservatives party manifesto""".split())
_ATTR_VERBS = set("""says say said saying reports reported found finds shows show showed shown voted votes announced
estimates estimated predicts predicted forecast forecasts warned warns admitted admits published concluded
confirmed confirms claimed claims promised pledged stated revealed reveals suggests suggested""".split())
_YOU_VERBS = {"voted", "votes", "promised", "pledged"}
_MOTION_SKIP = {"house", "believes", "regrets", "supports", "opposes", "prefers", "motion", "would", "should"}


def _base(tok: str) -> str:
    return _POSS.sub("", tok)


def _is_year(tok: str) -> bool:
    return bool(_YEAR.fullmatch(tok))


def numbers(text: str) -> set[str]:
    return {m.group().replace(",", "") for m in _NUMBER.finditer(text or "")}


def has_quantity(text: str) -> bool:
    """A number that is a quantity (not a bare year), a fraction word, a currency amount, ..."""
    if QTY.search(text or ""):
        return True
    return any(not _is_year(n) for n in numbers(text))


def _has_year(text: str) -> bool:
    return any(_is_year(n) for n in numbers(text))


def _stem(w: str) -> str:
    w = _base(w.lower())
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
        return w[:-1] if len(w) > 2 and w[-1] == w[-2] else w
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def stems(text: str) -> set[str]:
    return {_stem(t) for t in _TOKEN.findall(text or "") if t[0].isalpha() and len(t) > 2
            and _base(t.lower()) not in STOP}


def _motion_stems(motion: str) -> set[str]:
    return {s for s in stems(motion) if s not in _MOTION_SKIP}


def _has_proper(text: str) -> bool:
    toks = _TOKEN.findall(text or "")
    for i, t in enumerate(toks):
        b = _base(t)
        if re.fullmatch(r"[A-Z]{2,6}", b) and b != "OK":
            return True
        if i > 0 and re.fullmatch(r"[A-Z][a-z]+", b):
            return True
        if i == 0 and b != t and b[:1].isupper():          # sentence-initial possessive: "London's"
            return True
    return False


def _is_org(tok: str, idx: int) -> bool:
    b = _base(tok)
    return (b.lower() in _ORGS or (re.fullmatch(r"[A-Z]{2,6}", b) is not None and b != "OK")
            or (idx > 0 and re.fullmatch(r"[A-Z][a-z]+", b) is not None))


def named_source(text: str) -> bool:
    """An attribution verb ("says", "found", "according to", "voted") with a named body/person next to it."""
    toks = _TOKEN.findall(text or "")
    low = [t.lower() for t in toks]
    for i, w in enumerate(low):
        if w == "according" and i + 1 < len(low) and low[i + 1] == "to":
            window = range(i + 2, min(len(toks), i + 9))
        elif w in _ATTR_VERBS:
            window = range(max(0, i - 6), i)
        else:
            continue
        for j in window:
            if _is_org(toks[j], j) or (w in _YOU_VERBS and low[j] in ("you", "your", "you've")):
                return True
    return False


def classify(text: str, hedged: bool = False) -> str:
    """Taxonomy order (first match wins): value, attribution, prediction, statistic, comparison, causal, fact."""
    if VALUE.search(text):
        return "value"
    if named_source(text):
        return "attribution"
    if PRED.search(text):
        return "prediction"
    if has_quantity(text):
        return "statistic"
    if COMPARE.search(text):
        return "comparison"
    if CAUSAL.search(text):
        return "causal"
    return "value" if hedged else "fact"


def clean_text(s: str) -> tuple[str, bool]:
    """Strip leading filler ("Thank you.", "Honestly,", "And") and inline filler ("simply"); report hedges
    ("I think ...") so the type can still say value. Returns (text, hedged)."""
    s, prev = (s or "").strip(), None
    while s != prev:
        prev = s
        s = _LEAD_FREE.sub("", _LEAD_PUNCT.sub("", s, count=1), count=1).lstrip(" ,;:-–—")
    if _RHETORICAL.match(s):                       # "Isn't it true that X?" asserts X
        s = _RHETORICAL.sub("", s).rstrip("?").rstrip() + "."
    hedged = False
    m = _HEDGE.match(s)
    if m and len(s[m.end():].split()) >= 3:
        s, hedged = s[m.end():], True
    s = _INLINE.sub("", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,;:-")
    if not s:
        return "", hedged
    s = s[0].upper() + s[1:]
    if not _TERM_END.search(s):
        s += "."
    return s, hedged


def _has_clause_verb(s: str) -> bool:
    for t in _TOKEN.findall(s)[:7]:
        w = t.lower()
        if w in _AUX or w in _VERBS or (len(w) > 4 and w.endswith("ed")):
            return True
    return False


def clauses(sentence: str) -> list[str]:
    """Split compound sentences into independent clauses (", and road traffic is ...", ", because ...")."""
    out: list[str] = []
    for piece in re.split(r";\s+", sentence.strip()):
        rest = piece
        while True:
            for m in _CLAUSE.finditer(rest):
                conj, left, right = m.group(1).lower(), rest[:m.start()], rest[m.end():]
                if len(left.split()) < 4 or len(right.split()) < 4:
                    continue
                if conj in ("because", "while", "whereas") or _has_clause_verb(right):
                    out.append(left)
                    rest = f"but {right}" if conj == "but" else right   # keep "but" as a rebuttal cue
                    break
            else:
                out.append(rest)
                break
    return [c.strip() for c in out if c.strip()]


def heuristic_topic(text: str) -> str:
    """<= 4 words: the first run of >= 2 content words, else the first 3 content words."""
    toks = _TOKEN.findall(text or "")
    runs: list[list[str]] = []
    cur: list[str] = []
    for i, t in enumerate(toks):
        b = _base(t)
        w = b.lower()
        num = w[0].isdigit()
        skip = (w in STOP or w in _AUX or w in _VERBS or w.endswith("ly") or (len(w) < 3 and not num)
                or _is_year(w) or (len(w) > 4 and w.endswith("ed")))
        if skip:
            if cur:
                runs.append(cur)
            cur = []
            continue
        keep_case = b.isupper() or (b[:1].isupper() and (i > 0 or b != t))
        cur.append(b if keep_case else w)
    if cur:
        runs.append(cur)
    runs = [r for r in runs if any(not x[0].isdigit() and x.lower() not in _UNITS for x in r)]
    best = next((r for r in runs if len(r) >= 2), None)
    words = best[:4] if best else [x for r in runs for x in r][:3]
    return " ".join(words or [_base(t).lower() for t in toks if _base(t).lower() not in STOP][:3])


def _weight(stem: str, motion: set[str]) -> float:
    return 0.3 if stem in motion else 0.2 if stem in _GENERIC else 1.0


def rebuttal_link(clause: str, side: str, context: list, side_of: Callable[[Any], str],
                  motion: set[str]) -> list[tuple[str, str]]:
    """Attack the opponent claim this clause replies to: a shared number, else overlapping content words, else
    (rebuttal opener right after the opponent spoke) their most recent eligible claim."""
    raw = clause.strip()
    start = bool(REBUT_START.match(raw))
    strong = start or bool(REBUT_ANY.search(raw))
    if not (strong or CONTRAST.search(raw)) or not context:
        return []
    opp = [c for c in context if side_of(c) != side]
    if not opp:
        return []
    nums, sts = numbers(raw), stems(raw)
    best, best_score = None, 0.0
    for c in opp:                                  # oldest first: later claims win ties
        score = 3.0 * len(nums & numbers(c.text)) + sum(_weight(s, motion) for s in sts & stems(c.text))
        if score > 0 and score >= best_score:
            best, best_score = c, score
    if best is not None and (best_score >= 2.0 or (strong and best_score >= 1.0)):   # motion words alone: no
        return [(best.id, "attacks")]
    if start and side_of(context[-1]) != side:
        eligible = [c for c in opp if (getattr(c, "importance", 0) or 0) >= ELIGIBLE]
        if eligible:
            return [(eligible[-1].id, "attacks")]
    return []


def heuristic_drafts(sentence: str, side: str, context: Iterable[Any] = (),
                     side_of: Optional[Callable[[Any], str]] = None, motion: str = "") -> list[Draft]:
    """Deterministic, regex-only claim extraction for one sentence (no LLM, no clock)."""
    raw = (sentence or "").strip()
    if not raw or (raw.endswith("?") and not (named_source(raw) or (has_quantity(raw) and len(raw.split()) >= 4))):
        return []                                  # genuine questions ("Forty thousand?") aren't claims
    ctx = list(context)
    side_of = side_of or (lambda c: getattr(c, "side", None))
    mstems = _motion_stems(motion)
    proper = _has_proper(raw)
    out: list[Draft] = []
    for clause in clauses(raw):
        text, hedged = clean_text(clause)
        thin = len(stems(text)) < 2 and not (has_quantity(text) or _has_year(text) or named_source(text)
                                             or _has_proper(text))
        if not text or thin:                       # "That's exactly the problem." is not a claim
            continue
        typ = classify(text, hedged)
        qty, source = has_quantity(text), named_source(text)
        if typ in NO_CHECK:
            checkable = False
        elif typ in ("statistic", "attribution"):
            checkable = True
        else:
            anchor = qty or source or proper or _has_year(text) or MEASURE.search(text) or METRIC.search(text)
            checkable = bool(anchor) and not VAGUE.search(text)
        important = qty or source or bool(mstems & stems(text))
        out.append(Draft(text=text[:300], type=typ, checkable=checkable, importance=0.67 if important else 0.33,
                         topic=heuristic_topic(text), links=rebuttal_link(clause, side, ctx, side_of, mstems)))
    return out[:MAX_PER_SENTENCE]


# ============================================================================ LLM output mapping
def _truthy(v: Any) -> bool:
    """JSON-mode models also answer "yes", 1, "True"."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v >= 0.5
    return isinstance(v, str) and v.strip().strip(".!").lower() in ("true", "yes", "y", "1")


_IMPORTANCE_WORDS = {"low": 0.33, "medium": 0.67, "med": 0.67, "high": 1.0}


def map_importance(v: Any) -> float:
    """LLM 1..3 (or low/medium/high) -> 0.33/0.67/1.0; a 0..1 float passes through; anything else -> 0.5."""
    if isinstance(v, bool) or v is None:
        return 0.5
    if isinstance(v, str) and (w := v.strip().lower()) in _IMPORTANCE_WORDS:
        return _IMPORTANCE_WORDS[w]
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.5
    if x in (1.0, 2.0, 3.0):
        return IMPORTANCE[int(x)]
    if 0.0 <= x <= 1.0:
        return round(x, 2)
    return 1.0 if x > 3 else 0.5


def drafts_from_llm(out: Any, ctx_ids: set[str]) -> list[Draft]:
    """Validate/map one structured-output response. Raises LLMInvalid if the shape is wrong."""
    if isinstance(out, (str, bytes)):
        out = parse_json_text(out.decode() if isinstance(out, bytes) else out)
    if isinstance(out, dict) and isinstance(out.get("claims"), dict):     # JSON mode: one claim, not a list
        out = {**out, "claims": [out["claims"]] if out["claims"] else []}
    if not isinstance(out, dict) or not isinstance(out.get("claims"), list):
        raise LLMInvalid("response has no 'claims' list")
    drafts: list[Draft] = []
    for item in out["claims"][:MAX_PER_SENTENCE]:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        typ = str(item.get("type") or "other").strip().lower()
        typ = typ if typ in KNOWN_TYPES else "other"
        links, seen, raw_links = [], set(), item.get("links") or []
        for ln in [raw_links] if isinstance(raw_links, dict) else raw_links:   # JSON mode: one link, not a list
            if not isinstance(ln, dict):
                continue
            to = str(ln.get("to") or ln.get("claim_id") or "").strip()
            rel = str(ln.get("rel") or ln.get("relation") or "").strip().lower()
            rel = {"attack": "attacks", "support": "supports"}.get(rel, rel)
            if to in ctx_ids and rel in ("supports", "attacks") and to not in seen:
                links.append((to, rel))
                seen.add(to)
        topic = " ".join(str(item.get("topic") or "").split()[:4]) or heuristic_topic(text)
        drafts.append(Draft(text=text[:300], type=typ, checkable=_truthy(item.get("checkable")) and typ not in NO_CHECK,
                            importance=map_importance(item.get("importance")), topic=topic, links=links))
    if out["claims"] and not drafts:              # [] is the model saying "no claim"; junk items are not
        raise LLMInvalid(f"no usable claim in {len(out['claims'])} item(s): {str(out['claims'])[:80]!r}")
    return drafts


def _cause(e: BaseException) -> str:
    if isinstance(e, LLMError):
        return e.cause
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return "error"


def _speech_end(u: Utterance) -> float:
    """u.t_end, unless it is 0 or on another clock (stream-relative, skewed phone): then the bus ts."""
    t, ts = u.t_end or 0.0, u.ts or 0.0
    return t if t and (not ts or abs(ts - t) <= CLOCK_SKEW_S) else ts


def _pct(xs: list, q: float) -> Optional[int]:
    if not xs:
        return None
    xs = sorted(xs)
    return int(xs[min(len(xs) - 1, int(q * len(xs)))])


# ============================================================================ extractor
class ClaimExtractor:
    """stats: {"calls", "fallbacks", "by_cause": {timeout, error, refusal, invalid, stale, no_llm},
    "p50_ms", "p95_ms" (LLM attempts, None until the first), "llm": name or "heuristic"} + "cache_hit",
    "warmup" once known. Cumulative across sessions (process health). fallbacks == sum(by_cause)."""

    def __init__(self, argmap: ArgMap, publish: Callable[[Event], Any], llm: Optional[object] = None, *,
                 clock: Callable[[], float] = time.time):
        self.argmap, self.publish, self.llm, self.clock = argmap, publish, llm, clock
        self.gen = 0                                   # bumped by reset(): stale results are never published
        self._buf: dict[str, _Frag] = {}
        self._interims: dict[str, tuple] = {}          # speaker -> (time, (id, text)) of their last interim
        self._held: dict[str, float] = {}              # released unit id -> seconds the assembler sat on it
        self._sides: dict[str, str] = {}               # fallback side assignment if the argmap can't say
        self._motion = ""
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_loop: Any = None
        self._lat: deque = deque(maxlen=500)
        self.stats: dict = {"calls": 0, "fallbacks": 0, "by_cause": dict.fromkeys(CAUSES, 0),
                            "p50_ms": None, "p95_ms": None, "llm": getattr(llm, "name", None) or "heuristic"}

    # ------------------------------------------------------------------ session
    def reset(self) -> None:
        self.gen += 1
        for d in (self._buf, self._interims, self._held, self._sides):
            d.clear()

    async def warmup(self, motion: str) -> None:
        """One real tiny call: compiles the output schema and writes the prompt cache. Never raises."""
        self._motion = motion or ""
        if self.llm is None:
            return
        user = render_user(motion, [], [], "prop", WARMUP_SENTENCE)
        try:
            await asyncio.wait_for(self.llm.complete_json(SYSTEM_PROMPT, user, OUTPUT_SCHEMA, timeout=WARMUP_TIMEOUT_S,
                                                          max_tokens=LLM_MAX_TOKENS), WARMUP_TIMEOUT_S + 1.0)
            self.stats["warmup"] = "ok"
        except Exception as e:
            self.stats["warmup"] = f"failed:{_cause(e)}"
            log.warning("LLM warmup failed (%s): %s", _cause(e), e)

    # ------------------------------------------------------------------ sentence assembler
    def assemble(self, u: Utterance, now: float) -> list[Utterance]:
        """Final utterance -> units ending in sentence punctuation (buffered prefix prepended); the
        unpunctuated tail is buffered per speaker until it completes or flush_due() finds it quiet.
        Interims are accepted too (note_interim): they only keep the speaker's fragment open."""
        if not u.final:
            self.note_interim(u, now)
            return []
        text = (u.text or "").strip()
        if not text:
            return []
        f = self._buf.pop(u.speaker, None)
        head, tail = _split_complete(f"{f.text} {text}" if f else text)
        out = [self._unit(f.first if f else u, u, head)] if head else []
        if tail:
            keep = f is not None and not head
            frag = _Frag(f.first if keep else u, u, tail, f.t0 if keep else now, now, now)
            self._buf[u.speaker] = frag
            if len(tail.split()) >= FRAG_MAX_WORDS:      # no punctuation coming: don't sit on it
                out += self._release(u.speaker, now)
        return out

    def note_interim(self, u: Utterance, now: float) -> None:
        """The speaker is still talking: their fragment stays open. Only an interim that moved on counts:
        STTs resend empty/unchanged interims during silence."""
        text = (u.text or "").strip()
        if not text:
            return
        moved = self._interims.get(u.speaker, (0.0, None))[1] != (u.id, text)
        self._interims[u.speaker] = (now, (u.id, text))
        f = self._buf.get(u.speaker)
        if f is not None and moved:
            f.t_seen, f.paused_at = max(f.t_seen, now), None

    def on_pause(self, speaker: str, now: Optional[float] = None) -> list[Utterance]:
        """The fragment goes FRAG_PAUSE_* after this Pause unless speech resumes (never at once: a Pause also
        comes at a breath, or from run.py's PauseSynth between sparse interims). Returns overdue fragments."""
        f = self._buf.get(speaker)
        if f is None:
            return []
        now = self.clock() if now is None else now
        if f.paused_at is None:
            f.paused_at = now
        return self._release(speaker, now) if self._due(speaker, f, now) else []

    def flush_due(self, now: float) -> list[Utterance]:
        due = [s for s, f in self._buf.items() if self._due(s, f, now)]
        return [u for s in due for u in self._release(s, now)]

    def flush(self, speaker: Optional[str] = None, now: Optional[float] = None) -> list[Utterance]:
        """Release buffered fragments regardless of timers (one speaker, or all): end of input."""
        now = self.clock() if now is None else now
        return [u for s in ([speaker] if speaker is not None else list(self._buf)) for u in self._release(s, now)]

    def _due(self, speaker: str, f: _Frag, now: float) -> bool:
        if now - self._interims.get(speaker, (float("-inf"),))[0] <= INTERIM_LIVE_S:
            quiet, grace = FRAG_QUIET_S, FRAG_PAUSE_S
        else:
            quiet, grace = FRAG_QUIET_FINALS_S, FRAG_PAUSE_FINALS_S
        return (now - f.t_seen >= quiet or (f.paused_at is not None and now - f.paused_at >= grace)
                or now - f.t0 >= FRAG_MAX_AGE_S)

    def _release(self, speaker: str, now: float) -> list[Utterance]:
        f = self._buf.pop(speaker, None)
        if f is None:
            return []
        u = self._unit(f.first, f.last, f.text)
        if now > f.t_final:
            self._held[u.id] = now - f.t_final
        return [u]

    @staticmethod
    def _unit(first: Utterance, last: Utterance, text: str) -> Utterance:
        if first is last and text == (last.text or "").strip():
            return last
        return last.model_copy(update={"text": text, "t_start": first.t_start or last.t_start})

    # ------------------------------------------------------------------ extraction
    async def extract(self, u: Utterance) -> list[Claim]:
        gen = self.gen
        stale = self._age(u) - self._held.pop(u.id, 0.0) > float(getattr(config, "CLAIM_STALE_S", 6.0))
        sents = [s for s in split_sentences(u.text) if keep_sentence(s)]
        if not sents:
            return []
        side = self._side(u.speaker)
        res = await asyncio.gather(*(self._sentence(u, s, sents[:i], side, gen, stale)
                                     for i, s in enumerate(sents)))
        return [c for r in res for c in r]

    async def _sentence(self, u: Utterance, sent: str, earlier: list[str], side: str, gen: int,
                        stale: bool = False) -> list[Claim]:
        ctx = self._context(u.speaker)
        drafts: Optional[list[Draft]] = None
        cause = None
        if self.llm is None:
            cause = "no_llm"
        elif stale:
            cause = "stale"
        else:
            user = render_user(self._motion_text(), [(c.id, self._claim_side(c), c.text) for c in ctx],
                               self._earlier(u, earlier), side, sent)
            try:
                out = await asyncio.wait_for(self._call(user), float(getattr(config, "CLAIM_TIMEOUT_S", 2.5)))
                drafts = drafts_from_llm(out, {c.id for c in ctx})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                cause = _cause(e)
                log.log(logging.ERROR if cause == "error" else logging.WARNING,
                        "claim LLM fallback (%s) for %s: %s", cause, u.id, e)
        self.stats["calls"] += 1
        source = "llm"
        if drafts is None:
            self.stats["fallbacks"] += 1
            self.stats["by_cause"][cause or "error"] += 1
            drafts, source = heuristic_drafts(sent, side, ctx, self._claim_side, self._motion_text()), "heuristic"
        return self._emit(u, drafts, side, source, gen)

    async def _call(self, user: str) -> Any:
        """One LLM call in what CLAIM_TIMEOUT_S leaves after waiting for a slot. A call with no time to finish is
        never started (LLMTimeout, no tokens spent), and latency counts from the slot: queueing isn't the model."""
        budget = float(getattr(config, "CLAIM_TIMEOUT_S", 2.5))
        deadline = time.monotonic() + budget
        async with self._semaphore():
            left = deadline - time.monotonic()
            if left < min(MIN_CALL_S, budget / 2):
                raise LLMTimeout(f"waited {budget - left:.2f}s for an LLM slot, {left:.2f}s left")
            t0 = time.perf_counter()
            try:
                return await self.llm.complete_json(SYSTEM_PROMPT, user, OUTPUT_SCHEMA, timeout=left,
                                                    max_tokens=LLM_MAX_TOKENS)
            finally:
                self._note_latency((time.perf_counter() - t0) * 1000.0)

    def _emit(self, u: Utterance, drafts: list[Draft], side: str, source: str, gen: int) -> list[Claim]:
        out: list[Claim] = []
        t_end = _speech_end(u)
        for d in drafts[:MAX_PER_SENTENCE]:
            if self.gen != gen:                         # session reset while we were working
                break
            links = [Link(claim_id=cid, relation=rel) for cid, rel in d.links]
            dup = self._call_map("find_duplicate", u.speaker, d.text)
            if dup:
                self._call_map("merge_repeat", dup, links)   # same point again: no new check
                continue
            now = self.clock()
            marks = {k: float(v) for k, v in (("speech_end", t_end), ("stt_final", u.ts), ("claim", now)) if v}
            c = Claim(id=next_claim_id(), utterance_id=u.id, speaker=u.speaker, text=d.text, type=d.type,
                      checkable=d.checkable and d.type not in NO_CHECK, links=links, side=side,
                      importance=round(float(d.importance), 2), topic=d.topic, t_end=t_end, source=source,
                      marks=marks)
            self._call_map("add_claim", c)              # before publish: the next prompt sees this claim
            try:
                self.publish(c)
            except Exception:
                log.exception("publishing claim %s failed", c.id)
            out.append(c)
        return out

    # ------------------------------------------------------------------ helpers
    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem, self._sem_loop = asyncio.Semaphore(max(1, int(getattr(config, "CLAIM_CONCURRENCY", 6)))), loop
        return self._sem

    def _call_map(self, method: str, *args: Any) -> Any:
        """The argument map is a teammate-facing seam: a failure there must not lose the claim."""
        try:
            return getattr(self.argmap, method)(*args)
        except Exception:
            log.exception("argmap.%s failed", method)
            return None

    def _side(self, speaker: str) -> str:
        try:
            s = str(self.argmap.side_of(speaker) or "").lower()
        except Exception:
            s = ""
        if s in ("prop", "opp"):
            return s
        if speaker not in self._sides:                  # first seen -> prop, second -> opp, then alternate
            self._sides[speaker] = "prop" if len(self._sides) % 2 == 0 else "opp"
        return self._sides[speaker]

    def _claim_side(self, c: Any) -> str:
        s = str(getattr(c, "side", "") or "").lower()
        return s if s in ("prop", "opp") else self._side(getattr(c, "speaker", ""))

    def _context(self, speaker: str) -> list:
        try:
            ctx = [c for c in (self.argmap.context_for(speaker, limit=CONTEXT_LIMIT) or [])
                   if getattr(c, "id", None) and getattr(c, "text", None)]
        except Exception:
            log.debug("argmap.context_for unavailable", exc_info=True)
            return []

        def key(ic: tuple) -> tuple:
            m = re.fullmatch(r"c(\d+)", ic[1].id)
            return (int(m.group(1)) if m else 10 ** 9, ic[0])

        return [c for _, c in sorted(enumerate(ctx), key=key)][-CONTEXT_LIMIT:]

    def _earlier(self, u: Utterance, earlier: list[str]) -> list[str]:
        try:
            prev = list(self.argmap.turn_text(u.speaker, exclude_utterance_id=u.id) or [])
        except Exception:
            prev = []
        sents = [s for t in prev for s in split_sentences(str(t))]
        own = f" {_norm(u.text)} "
        while sents and f" {_norm(sents[-1])} " in own:   # a joined unit has its LAST final's id: the finals
            sents.pop()                                   # before it are its own opening fragments
        return (sents + list(earlier))[-2:]

    def _motion_text(self) -> str:
        return str(getattr(self.argmap, "motion", "") or self._motion or "")

    def _age(self, u: Utterance) -> float:
        t = _speech_end(u)
        return self.clock() - t if t else 0.0

    def _note_latency(self, ms: float) -> None:
        self._lat.append(ms)
        xs = list(self._lat)
        self.stats["p50_ms"], self.stats["p95_ms"] = _pct(xs, 0.5), _pct(xs, 0.95)
        usage = getattr(self.llm, "usage", None)
        if isinstance(usage, dict) and usage.get("calls"):
            self.stats["cache_hit"] = round(usage.get("cache_hits", 0) / usage["calls"], 2)
