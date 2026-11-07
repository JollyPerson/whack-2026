"""The verdict model. Owner: C. Uses its own key when one is set, so Brain and Evidence can't share rate limits."""
from __future__ import annotations

import os
from typing import Optional

from app.brain.llm import AnthropicLLM, make_llm


def make_verdict_llm() -> Optional[object]:
    key = os.environ.get("EVIDENCE_ANTHROPIC_API_KEY")
    model = os.environ.get("EVIDENCE_MODEL") or None
    if key:
        import anthropic
        return AnthropicLLM(anthropic.AsyncAnthropic(api_key=key, max_retries=0), model=model)
    return make_llm()
