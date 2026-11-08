"""The side bots' LLM calls: follow-up questions, banter and the own-knowledge check. Owner: B.

Built on Brain's client the way Evidence builds its verdict model (app/evidence/llm.py): with Claude, follow-ups
and banter use config.BOTS_MODEL (Haiku: fast) and the check uses config.BOTS_CHECK_MODEL (Opus: most accurate);
a non-Claude make_llm() serves all three as it is; no LLM at all leaves the bots on templates, with no check.
Every call is bounded (FAST_TIMEOUT_S, CHECK_TIMEOUT_S, including the wait for one of CONCURRENCY slots) and
returns None on any failure: the caller falls back to a template or to nothing. Wording rules live in the
prompts; app/bots/lines.guard() enforces them in code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from app import config
from app.brain.llm import AnthropicLLM, make_llm

log = logging.getLogger("bots.writer")

FAST_TIMEOUT_S = 4.0      # follow-up and banter (Haiku: ~1.4 s)
CHECK_TIMEOUT_S = 12.0    # the own-knowledge check (Opus; thinking can't be turned off, effort low keeps it short)
CONCURRENCY = 4
SIDE_NAME = {"prop": "Proposition", "opp": "Opposition"}
PERSONA_NAME = {"for": "FOR", "against": "AGAINST"}

_RULES = """\
- No insults, nothing about any person: not their looks, voice, manner, intelligence, honesty or character \
(never "did you make that up", "do you even know").
- Never the words lie, false, wrong, misleading, nonsense, incorrect, myth.
- No hashtags, emojis, quotation marks or URLs."""

FOLLOWUP_SYSTEM = f"""\
Two bots sit beside a live debate: FOR backs the Proposition, AGAINST backs the Opposition. Each one heckles \
only the opposing debater, in a short speech bubble on screen.

Write ONE pointed follow-up question addressed directly to the opposing debater ("you") about the claim they \
just made.
- Dry and sharp, like a good heckle. One short question of at most 10 words: the bubble cuts anything longer.
- Ask for what is missing: a source, a definition, a baseline, a mechanism, a consequence. Vary the angle, and \
don't open the way a line already said did.
- Add no facts, figures, dates, names or sources of your own. You may repeat a figure from the claim.
{_RULES}
Return JSON: {{"question": "..."}}"""

CHECK_SYSTEM = f"""\
You check one claim from a live debate against your own knowledge. You cannot search, and your answer is shown \
without anyone verifying it, so be conservative.

assessment:
- "likely_false": you are confident the claim is materially inaccurate: a figure far from the established \
value, an event that did not happen, a quote or decision attributed to the wrong person or body.
- "likely_true": it matches what you know.
- "unsure": anything else: recent or fast-changing figures, local or obscure facts, predictions, opinions, vague \
claims, rounding or definitional quibbles.
confidence: the probability, from 0 to 1, that your assessment is right.
correction: if likely_false, a phrase of at most 10 words that completes "Last I checked, ...", with the figure if \
there is one (e.g. "it was nearer 4%"); otherwise "".
challenge: if likely_false, one heckle of at most 12 words addressed directly to the speaker ("you"), questioning \
the claim with what you believe is right, dry and a little humble ("6%? Last I checked it was nearer 4%."); \
otherwise "".
{_RULES}
Return JSON only."""

BANTER_SYSTEM = f"""\
Two AI bots sit beside a live debate, each with its own speech bubble: FOR backs the Proposition, AGAINST backs \
the Opposition. Now and then they spar with each other: dry, witty and good-natured, like two seconds trading \
quips at ringside.

You are one of the bots. The other bot just posted a line aimed at your side's debater. Reply to the other bot \
(not to the debaters) with ONE dry, witty sentence of at most 10 words: the bubble cuts anything longer.
- Tease the other bot's line, its style or its confidence. Keep it between the two bots: never mention the \
debaters, the speakers, the sides or any other people ("you" is the other bot).
- Don't open the way an earlier line did.
- No numbers (not even in words), facts, statistics, names or sources, and no new claims about the topic.
{_RULES}
- If nothing good and safe comes to mind, return an empty line.
Return JSON: {{"line": "..."}}"""

FOLLOWUP_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}
BANTER_SCHEMA = {"type": "object", "properties": {"line": {"type": "string"}}, "required": ["line"]}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string", "enum": ["likely_false", "likely_true", "unsure"]},
        "confidence": {"type": "number"},
        "correction": {"type": "string"},
        "challenge": {"type": "string"},
    },
    "required": ["assessment", "confidence", "correction", "challenge"],
}


@dataclass
class Check:
    likely_false: bool
    confidence: float
    challenge: str = ""
    correction: str = ""


def _get(o: Any, k: str, default: Any = "") -> Any:
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def _conf(v: Any) -> float:
    """0..1: 0.9, 90 and "90%" all mean 0.9; anything unreadable is 0."""
    try:
        x = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0
    x = x / 100.0 if x > 1 else x
    return min(max(x, 0.0), 1.0) if x == x else 0.0


def _claim_text(claim: Any) -> str:
    return " ".join(str(_get(claim, "text") or "").split())


class Writer:
    def __init__(self, fast: Any = None, check: Any = None, concurrency: int = CONCURRENCY):
        self.fast, self.checker = fast, check
        self.slots = asyncio.Semaphore(concurrency)

    @property
    def name(self) -> str:
        if self.fast is None:
            return "templates only (no LLM)"
        return f"{getattr(self.fast, 'name', '?')}, check {getattr(self.checker, 'name', None) or 'off'}"

    async def _call(self, llm: Any, system: str, user: str, schema: dict, timeout: float,
                    max_tokens: int = 200) -> Optional[dict]:
        """One bounded call: `timeout` covers the wait for a slot too. None on any failure (logged)."""
        if llm is None:
            return None
        deadline = time.monotonic() + timeout
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout)
        except TimeoutError:
            log.info("bots: no LLM slot within %.0f s", timeout)
            return None
        try:
            left = deadline - time.monotonic()
            if left < 0.5:
                return None
            return await asyncio.wait_for(llm.complete_json(system, user, schema, left, max_tokens=max_tokens), left)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("bots: %s call failed: %r", getattr(llm, "name", "llm"), e)
            return None
        finally:
            self.slots.release()

    async def followup(self, claim: Any, motion: str = "", persona: str = "", granted: bool = False,
                       avoid: tuple = ()) -> str:
        """A pointed question to the opponent about `claim` (raw: the caller guards it), or ""."""
        side = SIDE_NAME.get(str(_get(claim, "side") or ""), "opposing")
        note = ("\nThe claim checks out against a source: question its relevance to the motion, not its truth."
                if granted else "")
        seen = "\nAlready said (don't repeat): " + " | ".join(avoid[-6:]) if avoid else ""
        user = (f"Motion: {motion or '(not given)'}\nYou are: {PERSONA_NAME.get(persona, persona)}\n"
                f"The {side} debater just claimed ({_get(claim, 'type') or 'other'}): {_claim_text(claim)}{note}{seen}")
        out = await self._call(self.fast, FOLLOWUP_SYSTEM, user, FOLLOWUP_SCHEMA, FAST_TIMEOUT_S)
        return str(_get(out, "question") or "") if out else ""

    async def banter(self, line: str, persona: str, other: str, avoid: tuple = ()) -> str:
        """`persona`'s dry reply to the `other` bot's `line` (raw: the caller guards it), or ""."""
        me, them = PERSONA_NAME.get(persona, persona), PERSONA_NAME.get(other, other)
        seen = "\nEarlier banter (don't repeat): " + " | ".join(avoid[-4:]) if avoid else ""
        user = f"You are {me}. The {them} bot just said: {line}{seen}"
        out = await self._call(self.fast, BANTER_SYSTEM, user, BANTER_SCHEMA, FAST_TIMEOUT_S)
        return str(_get(out, "line") or "") if out else ""

    async def check(self, claim: Any, motion: str = "") -> Optional[Check]:
        """The own-knowledge check of `claim`, or None (no check model, timeout, error)."""
        side = SIDE_NAME.get(str(_get(claim, "side") or ""), "a")
        user = (f"Today is {time.strftime('%d %B %Y')}. Motion: {motion or '(not given)'}\n"
                f"Claim ({_get(claim, 'type') or 'other'}) by the {side} speaker: {_claim_text(claim)}")
        out = await self._call(self.checker, CHECK_SYSTEM, user, CHECK_SCHEMA, CHECK_TIMEOUT_S, max_tokens=300)
        if not out:
            return None
        return Check(likely_false=_get(out, "assessment") == "likely_false", confidence=_conf(_get(out, "confidence")),
                     challenge=str(_get(out, "challenge") or ""), correction=str(_get(out, "correction") or ""))


def make_writer() -> Writer:
    """Brain's LLM client with the bots' models (see the module docstring). Blocking (SDK import): run in a thread."""
    shared = make_llm()
    if isinstance(shared, AnthropicLLM):
        fast = shared if shared.model == config.BOTS_MODEL else AnthropicLLM(shared.client, model=config.BOTS_MODEL)
        return Writer(fast, AnthropicLLM(shared.client, model=config.BOTS_CHECK_MODEL))
    return Writer(shared, shared)
