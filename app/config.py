"""Runtime flags + tunables. Owner: B. Every value can be overridden by an environment variable
of the same name (e.g. DIRECTOR_MIN_GAP_S=20 ./run.sh drive). Modules read these at decision time."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env for every entry point (run.sh, scripts, python -m ...). Never under pytest: tests stay offline.
if "pytest" not in sys.modules and os.environ.get("DEBATE_REF_NO_DOTENV") != "1":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except ImportError:
        pass


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


# Runtime kill switches, flipped by Control events (dashboard ?admin=1 or POST /control).
FLAGS: dict = {
    "tts": True,             # D: speak interjections
    "director": True,        # B: allow interjections at all
    "web_fallback": True,    # C: allow slow web search
    "verdict_cache": True,   # C: answer a claim already settled on this Motion from cache/verdicts, without checking it again
    "deep_check": False,     # C: second, slower check in the background (about 30 s), as a via="deep" Verdict. Off until it beats the fast check on Opus (on Kimi it fixed 0, broke 1)
    "interims": True,        # D: show interim text
    "agent_speaking": False, # set by the browser around TTS playback (echo guard)
}

# --- brain: claim extraction ----------------------------------------------------------------
LLM_PROVIDER = _env("LLM_PROVIDER", "auto")               # auto | anthropic (claude) | openai (chatgpt) | heuristic
LLM_MODEL = _env("LLM_MODEL", "claude-haiku-5-5")          # OpenAI: set OPENAI_MODEL (default gpt-6-luna)
LLM_BASE_URL = _env("LLM_BASE_URL", "")                    # OpenAI-compatible endpoint (openai provider)
LLM_THINKING = _env("LLM_THINKING", "")                    # Claude: disabled | adaptive | between_tools | omit
LLM_FALLBACKS = _env("LLM_FALLBACKS", True)                # Claude: server-side refusal fallbacks where supported
CLAIM_TIMEOUT_S = _env("CLAIM_TIMEOUT_S", 2.5)             # then heuristic fallback (Haiku 5.5 measured p95 ~1.5 s)
CLAIM_STALE_S = _env("CLAIM_STALE_S", 6.0)                 # older speech skips the LLM
CLAIM_CONCURRENCY = _env("CLAIM_CONCURRENCY", 6)

# --- brain: argument map ----------------------------------------------------------------------
ARGMAP_K_TURNS = _env("ARGMAP_K_TURNS", 1)                 # opponent turns before "unanswered"
ARGMAP_SNAPSHOT_HZ = _env("ARGMAP_SNAPSHOT_HZ", 4.0)
ARGMAP_MIN_TURN_S = _env("ARGMAP_MIN_TURN_S", 8.0)         # a turn this long counts even with no claims
ARGMAP_MIN_NR_TURN_S = _env("ARGMAP_MIN_NR_TURN_S", 15.0)  # provisional non-responsive check after this
ARGMAP_BACKCHANNEL_WORDS = _env("ARGMAP_BACKCHANNEL_WORDS", 3)   # "No!" / "Yeah." doesn't take the floor

# --- brain: pause synthesis (if A's VAD Pause is missing or late) ----------------------------
PAUSE_SYNTH = _env("PAUSE_SYNTH", "auto")                  # auto | on | off
PAUSE_SYNTH_GAP_S = _env("PAUSE_SYNTH_GAP_S", 1.2)

# --- brain: director --------------------------------------------------------------------------
DIRECTOR_MIN_GAP_S = _env("DIRECTOR_MIN_GAP_S", 30.0)
DIRECTOR_MIN_CONF = _env("DIRECTOR_MIN_CONF", 0.75)
DIRECTOR_AGGR = _env("DIRECTOR_AGGR", 3)                   # 1 (quiet) .. 5 (chatty)
DIRECTOR_VOICE_NONRESPONSIVE = _env("DIRECTOR_VOICE_NONRESPONSIVE", False)   # default: dashboard only

# --- evidence (C) defaults the mocks also honour ----------------------------------------------
STALE_CLAIM_S = _env("STALE_CLAIM_S", 12.0)
VERDICT_TIMEOUT_S = _env("VERDICT_TIMEOUT_S", 8.0)
