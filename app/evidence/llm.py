"""The verdict model. Owner: C. Uses its own key when one is set, so Brain and Evidence can't share rate limits.

Verdicts default to Opus for accuracy; claim extraction stays on Brain's faster LLM_MODEL. EVIDENCE_MODEL overrides.
"""
from __future__ import annotations

import os
from typing import Optional

from app.brain.llm import AnthropicLLM, make_llm

DEFAULT_VERDICT_MODEL = "claude-opus-5-5"


def make_verdict_llm() -> Optional[object]:
    key = os.environ.get("EVIDENCE_ANTHROPIC_API_KEY")
    model = os.environ.get("EVIDENCE_MODEL") or DEFAULT_VERDICT_MODEL
    if key:
        import anthropic
        return AnthropicLLM(anthropic.AsyncAnthropic(api_key=key, max_retries=0), model=model)
    shared = make_llm()
    if isinstance(shared, AnthropicLLM):          # same credentials as Brain, but the verdict model
        return AnthropicLLM(shared.client, model=model)
    return shared
