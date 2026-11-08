"""The verdict model. Owner: C. Uses its own key when one is set, so Brain and Evidence can't share rate limits."""
from __future__ import annotations

import logging
import os
from typing import Optional

from app import config
from app.brain.llm import ANTHROPIC_DEFAULT_MODEL, CLAUDE_MODEL_ID, AnthropicLLM, make_llm

log = logging.getLogger("evidence.llm")


def make_verdict_llm() -> Optional[object]:
    key = os.environ.get("EVIDENCE_ANTHROPIC_API_KEY")
    model = os.environ.get("EVIDENCE_MODEL", "").strip()
    if model and not CLAUDE_MODEL_ID.fullmatch(model):      # an alias ("opus") or "[1m]" suffix the API rejects
        log.warning("EVIDENCE_MODEL=%r is not a Claude model id; ignoring it", model)
        model = ""
    if key:
        import anthropic
        if not model:                                       # LLM_MODEL, checked the way Brain checks it
            brain = str(config.LLM_MODEL or "").strip()
            model = brain if CLAUDE_MODEL_ID.fullmatch(brain) else ANTHROPIC_DEFAULT_MODEL
        return AnthropicLLM(anthropic.AsyncAnthropic(api_key=key, max_retries=0), model=model)
    llm = make_llm()
    if model and isinstance(llm, AnthropicLLM) and llm.model != model:
        return AnthropicLLM(llm.client, model=model)        # Brain's credentials, Evidence's model
    return llm
