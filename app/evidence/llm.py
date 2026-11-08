"""The verdict model. Owner: C. Uses its own key when one is set, so Brain and Evidence can't share rate limits.

Verdicts default to Opus for accuracy; claim extraction stays on Brain's faster LLM_MODEL. EVIDENCE_MODEL overrides.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from app.brain.llm import CLAUDE_MODEL_ID, AnthropicLLM, make_llm

log = logging.getLogger("evidence.llm")

DEFAULT_VERDICT_MODEL = "claude-opus-5-5"


def make_verdict_llm() -> Optional[object]:
    key = os.environ.get("EVIDENCE_ANTHROPIC_API_KEY")
    model = os.environ.get("EVIDENCE_MODEL", "").strip()
    if model and not CLAUDE_MODEL_ID.fullmatch(model):      # an alias ("opus") or "[1m]" suffix the API rejects
        log.warning("EVIDENCE_MODEL=%r is not a Claude model id; using %s", model, DEFAULT_VERDICT_MODEL)
        model = ""
    model = model or DEFAULT_VERDICT_MODEL
    if key:
        import anthropic
        return AnthropicLLM(anthropic.AsyncAnthropic(api_key=key, max_retries=0), model=model)
    shared = make_llm()
    if isinstance(shared, AnthropicLLM) and shared.model != model:   # same credentials as Brain, the verdict model
        return AnthropicLLM(shared.client, model=model)
    return shared
