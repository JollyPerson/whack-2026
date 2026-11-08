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


TRUE_WORDS, FALSE_WORDS = {"1", "true", "yes", "on"}, {"0", "false", "no", "off", ""}


def _warn(name: str, raw: str, used) -> None:
    """Loud, not silent: during venue tuning a typo must not look like it took effect. stderr, because config is
    imported before logging is set up."""
    print(f"config: ignoring {name}={raw!r} (not a valid {type(used).__name__}): using {used!r}", file=sys.stderr)


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if isinstance(default, bool):
        word = raw.strip().lower()
        if word not in TRUE_WORDS | FALSE_WORDS:
            _warn(name, raw, False)
        return word in TRUE_WORDS
    try:
        if isinstance(default, int):
            try:
                return int(raw)
            except ValueError:
                f = float(raw)                       # "4.0" is a fine int; "4.5" is not
                if not f.is_integer():
                    raise
                return int(f)
        return type(default)(raw)
    except (TypeError, ValueError, OverflowError):
        if raw.strip():                              # an empty value is just "not set"
            _warn(name, raw, default)
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
    "bots": True,            # B: side bots may post (only while the bots module runs: BOTS=real)
}

# --- side bots (app/bots): For/Against seconds shown as Mate Engine speech bubbles. Enable with BOTS=real -------
BOTS_MIN_GAP_S = _env("BOTS_MIN_GAP_S", 10.0)            # per bot: at most one bubble this often
BOTS_LINE_TTL_S = _env("BOTS_LINE_TTL_S", 30.0)          # a queued line older than this is dropped
BOTS_VERDICT_WAIT_S = _env("BOTS_VERDICT_WAIT_S", 10.0)  # wait this long for a sourced Verdict before an unsourced check
BOTS_CHECK_MIN_CONF = _env("BOTS_CHECK_MIN_CONF", 0.85)  # the AI-knowledge challenge only fires at or above this
BOTS_BANTER = _env("BOTS_BANTER", True)                  # bot-to-bot replies (dry, guarded, rate-limited)
BOTS_BANTER_GAP_S = _env("BOTS_BANTER_GAP_S", 45.0)      # at most one banter line this often, across both bots
BOTS_MODEL = _env("BOTS_MODEL", "claude-haiku-5-5")      # follow-ups and banter: fast
BOTS_CHECK_MODEL = _env("BOTS_CHECK_MODEL", "claude-opus-5-5")   # the unsourced knowledge check: most accurate

# --- judge (app/judge): post-debate summary + verdict on the Final screen click. Enable with JUDGE=real ----------
JUDGE_MODEL = _env("JUDGE_MODEL", "claude-opus-5-5")     # weighs the scorecard + transcript, names the victor
JUDGE_TIMEOUT_S = _env("JUDGE_TIMEOUT_S", 60.0)          # then the scorecard formula decides (DebateResult.via=formula)
JUDGE_DIR = _env("JUDGE_DIR", "logs/summaries")          # <run>-summary.md + .json
JUDGE_DANCE_WIN = _env("JUDGE_DANCE_WIN", "MMD-Conqueror")             # alternative: MMD-KING
JUDGE_DANCE_LOSE = _env("JUDGE_DANCE_LOSE", "MMD-Lost One_s Weeping")  # alternative: MMD-Failure Girl
JUDGE_DANCE_DRAW = _env("JUDGE_DANCE_DRAW", "MMD-皆大欢喜-卜逻desu")       # both bots, on a draw
JUDGE_DANCE_S = _env("JUDGE_DANCE_S", 30.0)              # the dances stop after this long (or at the next SessionStart)
JUDGE_MUSIC = _env("JUDGE_MUSIC", "winner")              # winner | none | both (a draw with "winner": silent)

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

# --- brain: consistency checker (self-contradictions, concessions, agreement) + end-of-debate summary -----------
CONSISTENCY = _env("CONSISTENCY", True)                     # off: no checks, no findings, the summary still works
CONSISTENCY_MODEL = _env("CONSISTENCY_MODEL", "")           # "" = LLM_MODEL (Haiku); profile test-haiku pins Haiku
SUMMARY_MODEL = _env("SUMMARY_MODEL", "")                   # "" = CONSISTENCY_MODEL / LLM_MODEL
CONSISTENCY_DELAY_S = _env("CONSISTENCY_DELAY_S", 3.0)      # after a turn closes, so its last claims have landed
CONSISTENCY_MIN_CONF = _env("CONSISTENCY_MIN_CONF", 0.7)    # findings below this are dropped
CONSISTENCY_TIMEOUT_S = _env("CONSISTENCY_TIMEOUT_S", 8.0)  # then the heuristic answers for that turn
CONSISTENCY_VOICE = _env("CONSISTENCY_VOICE", False)        # the referee may SAY U-turns and concessions (else screen only)
DIRECTOR_DRY_FIRST_S = _env("DIRECTOR_DRY_FIRST_S", 25.0)  # canned dry-spell line; 0 = off

# --- evidence (C) defaults the mocks also honour ----------------------------------------------
STALE_CLAIM_S = _env("STALE_CLAIM_S", 12.0)
VERDICT_TIMEOUT_S = _env("VERDICT_TIMEOUT_S", 8.0)
