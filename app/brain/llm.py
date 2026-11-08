"""LLM clients for claim extraction. Owner: B.

make_llm() picks a client from config.LLM_PROVIDER (env-overridable, so the team can switch without code):
  auto (default)       Claude if Anthropic credentials exist, else ChatGPT/OpenAI if OPENAI_API_KEY is set
                       (ChatGPT first when OPENAI_MODEL, or a non-Claude LLM_MODEL, asks for a GPT model).
  anthropic | claude   AsyncAnthropic(max_retries=0), model config.LLM_MODEL ("claude-haiku-5-5"; ANTHROPIC_MODEL
                       is ignored: it is Claude Code's setting), thinking off,
                       effort low, structured outputs (output_config.format json_schema), cached system prompt.
  openai | chatgpt     OpenAI Chat Completions (OPENAI_MODEL, default gpt-6-luna; reasoning_effort none,
                       max_completion_tokens, strict json_schema) or any compatible server via LLM_BASE_URL
                       (OpenRouter, Groq, vLLM, Ollama, ...), self-adapting to parameters a server rejects.
  heuristic            None -> claims.py runs its regex extractor.
No key, no SDK, or an unknown provider also returns None: the brain always runs. Teammates (e.g. C's verdict
step) can reuse make_llm().complete_json(system, user, schema, timeout) with any JSON schema: both clients send
strict_schema(schema), so the same schema behaves the same on Claude and ChatGPT (optional fields come back
as null; min/max-style constraints become hints in the description, so check them in code).

complete_json() never takes longer than `timeout` and raises LLMTimeout / LLMRefusal / LLMInvalid / LLMError;
each carries `.cause`, which is the key the extractor counts fallbacks under. Each client keeps token/cache
counters in `.usage`; pass usage_out={} to get one call's counts (safe under concurrency).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional, Protocol

from app import config

log = logging.getLogger("brain.llm")

SCHEMA_NAME = "claims"
THINKING_MIN_TOKENS = 2048                     # max_tokens floor whenever the model reasons before answering
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-5-5"   # the claude-api skill's fast/cheap extraction tier
CLAUDE_MODEL_ID = re.compile(r"claude-[a-z0-9.-]+")   # rejects Claude Code aliases ("opus") and "[1m]" suffixes
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models the claude-api skill says should opt into server-side refusal fallbacks (Haiku 5.5 has none).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Models that accept thinking {"type": "disabled"} at effort low; Sonnet 5.5 turns thinking off differently.
THINKING_OFF_MODELS = {"claude-haiku-5-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7"}
# claude-api skill: Haiku 4.5 has no effort parameter and effort errors on Sonnet 4.5; older models predate it.
# Prefixes, so dated ids (claude-haiku-4-5-20251001, claude-sonnet-4-20250514) match too.
NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4-0", "claude-sonnet-4-2",
                    "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-2", "claude-3")
# Where omitting `thinking` means no thinking (4.6 and older), so max_tokens needs no room for it.
NO_THINKING_BY_DEFAULT = NO_EFFORT_MODELS + ("claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-6")


class LLMError(Exception):
    """API/transport failure (HTTP error, connection, rate limit)."""
    cause = "error"


class LLMTimeout(LLMError, TimeoutError):
    cause = "timeout"


class LLMRefusal(LLMError):
    """The model declined (Anthropic stop_reason "refusal", OpenAI refusal / content_filter)."""
    cause = "refusal"


class LLMInvalid(LLMError):
    """Output unusable: not JSON, not an object, or cut short (stop_reason max_tokens etc.)."""
    cause = "invalid"


class LLM(Protocol):
    name: str

    async def complete_json(self, system: str, user: str, schema: dict, timeout: float,
                            max_tokens: int = 400) -> dict:
        """Return the parsed JSON object matching `schema`. Raise on timeout / API error / bad JSON."""
        ...


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_json_text(text: Any) -> dict:
    """JSON object from model text (tolerates ``` fences some OpenAI-compatible servers add)."""
    if not isinstance(text, str) or not text.strip():
        raise LLMInvalid("empty response text")
    try:
        data = json.loads(_FENCE.sub("", text))
    except ValueError as e:
        raise LLMInvalid(f"invalid JSON ({e}): {text[:80]!r}") from e
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]                      # some servers (Kimi on Canopy Wave) wrap the object in a list
    if not isinstance(data, dict):
        raise LLMInvalid(f"expected a JSON object, got {type(data).__name__}")
    return data


def _new_usage() -> dict:
    return {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0}


def usage_line(llms: dict) -> str:
    """'claims=claude-haiku-5-5 12 calls 8.1k in (5.2k cached) 1.3k out; ...' for the run's cost report."""
    k = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)     # noqa: E731
    parts, seen = [], set()
    for label, llm in llms.items():
        u = getattr(llm, "usage", None)
        if not u or id(llm) in seen:
            continue
        seen.add(id(llm))
        parts.append(f"{label}={getattr(llm, 'model', '?')} {u.get('calls', 0)} calls "
                     f"{k(u.get('input_tokens', 0))} in ({k(u.get('cache_read_tokens', 0))} cache read, "
                     f"{k(u.get('cache_write_tokens', 0))} cache write) {k(u.get('output_tokens', 0))} out")
    return "; ".join(parts) or "no LLM calls"


def _num(v: Any) -> int:
    return v if isinstance(v, int) else 0


def _tally(delta: dict, *into: Optional[dict]) -> None:
    for d in into:
        if d is not None:
            for k, v in delta.items():
                d[k] = d.get(k, 0) + v


# ----------------------------------------------------------------------------- strict JSON schema
# Keywords structured outputs reject (Claude: no numeric/string/array-size constraints; OpenAI strict mode
# rejects most of the same). They move into the description as a hint, like the anthropic SDK's .parse().
_SCHEMA_HINTS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
    "pattern", "minItems", "maxItems", "uniqueItems", "contains", "minContains", "maxContains", "minProperties",
    "maxProperties", "patternProperties", "propertyNames", "unevaluatedProperties", "unevaluatedItems",
    "dependentRequired", "dependentSchemas"})
_SCHEMA_FORMATS = frozenset({"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"})
_STRICT_CACHE: dict[int, tuple[Any, Any]] = {}


def _nullable(p: Any) -> Any:
    if not isinstance(p, dict):
        return p
    t = p.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t) or \
            any(isinstance(x, dict) and x.get("type") == "null" for x in p.get("anyOf") or []):
        return p
    return {"anyOf": [p, {"type": "null"}]}


def _strict(s: Any) -> Any:
    if isinstance(s, list):
        return [_strict(x) for x in s]
    if not isinstance(s, dict):
        return s
    out: dict = {}
    hints = []
    for k, v in s.items():
        if k in _SCHEMA_HINTS or (k == "format" and v not in _SCHEMA_FORMATS):
            hints.append(f"{k}: {json.dumps(v)}")
        elif k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: _strict(sub) for name, sub in v.items()}       # keys are names, not keywords
        elif k in ("items", "anyOf", "oneOf", "allOf", "prefixItems"):
            out["anyOf" if k == "oneOf" else k] = _strict(v)
        else:
            out[k] = v                                     # type/enum/const/required/description...: values
    props, t = out.get("properties"), out.get("type", "object")
    if isinstance(props, dict) and (t == "object" or (isinstance(t, list) and "object" in t)):
        req = [n for n in dict.fromkeys(out.get("required") or []) if n in props]
        out["properties"] = {n: (p if n in req else _nullable(p)) for n, p in props.items()}
        out["required"] = req + [n for n in props if n not in req]
        out["additionalProperties"] = False
    if hints:
        desc = out.get("description")
        out["description"] = (f"{desc}\n\n" if desc else "") + "{" + ", ".join(hints) + "}"
    return out


def strict_schema(schema: Any) -> Any:
    """`schema` as both providers' structured outputs accept it: additionalProperties false and every property
    required on each object with properties (optional ones become nullable), oneOf -> anyOf, unsupported
    constraints moved into the description. Idempotent; an already-strict schema (OUTPUT_SCHEMA) comes back
    equal. Cached per schema object, so treat schemas as immutable once passed in."""
    hit = _STRICT_CACHE.get(id(schema))
    if hit is not None and hit[0] is schema:
        return hit[1]
    out = _strict(schema)
    if len(_STRICT_CACHE) >= 64:              # callers building a fresh dict per call
        _STRICT_CACHE.clear()
    _STRICT_CACHE[id(schema)] = (schema, out)  # holding `schema` keeps its id from being reused
    return out


def thinking_for(model: str) -> Optional[dict]:
    """Thinking config that is valid for `model` at effort low (None = omit the parameter).
    LLM_THINKING=disabled|adaptive|between_tools|omit overrides."""
    forced = str(getattr(config, "LLM_THINKING", os.environ.get("LLM_THINKING", "")) or "").strip().lower()
    if forced in ("disabled", "adaptive", "between_tools"):
        return {"type": forced}
    if forced == "omit":
        return None
    if model in THINKING_OFF_MODELS:
        return {"type": "disabled"}
    if model == "claude-sonnet-5-5":
        return {"type": "between_tools"}
    return None            # Opus 5.5 / Fable: thinking can't be disabled; effort low keeps it short


# ----------------------------------------------------------------------------- Anthropic
def _claude_model(model: Any = None) -> str:
    """The Claude model id to send: `model`, else config.LLM_MODEL, if it is a valid id; otherwise
    ANTHROPIC_DEFAULT_MODEL. LLM_MODEL may hold a GPT name (how ChatGPT is picked for claims), and Claude Code
    style values ("opus", "claude-opus-5-5[1m]") are rejected by the API."""
    explicit = str(model or "").strip()
    m = explicit or str(config.LLM_MODEL or "").strip()
    if CLAUDE_MODEL_ID.fullmatch(m):
        return m
    if explicit or m.lower().startswith("claude"):   # a GPT name in LLM_MODEL is expected; a broken id is not
        log.warning("%r is not a Claude model id; using %s", m, ANTHROPIC_DEFAULT_MODEL)
    return ANTHROPIC_DEFAULT_MODEL


class AnthropicLLM:
    supports_blocks = True     # `user` may be a list of content blocks, so callers can mark a growing prefix cacheable

    def __init__(self, client: Any = None, model: Optional[str] = None, effort: str = "low"):
        if client is None:
            import anthropic
            client = anthropic.AsyncAnthropic(max_retries=0)   # SDK retries would multiply wall time
        self.client = client
        self.model = _claude_model(model)              # also for clients built directly (C's verdict model)
        self.name = f"anthropic:{self.model}"
        self.effort = effort
        self.thinking = thinking_for(self.model)
        self.fallbacks = self.model in FALLBACK_MODELS and bool(getattr(config, "LLM_FALLBACKS", True))
        self.usage = _new_usage()

    def request(self, system: str, user: str, schema: dict, max_tokens: int = 400) -> dict:
        """messages.create kwargs. Thinking tokens count toward max_tokens, so leave room when it is on.
        Structured outputs work on every served 4.x+ model; effort only where the model accepts it."""
        if self.thinking is None:
            off = self.model.startswith(NO_THINKING_BY_DEFAULT)
        else:
            off = self.thinking.get("type") in ("disabled", "between_tools")
        output_config: dict = {"format": {"type": "json_schema", "schema": strict_schema(schema)}}
        if self.effort and not self.model.startswith(NO_EFFORT_MODELS):
            output_config = {"effort": self.effort, **output_config}
        kw: dict = {
            "model": self.model,
            "max_tokens": max_tokens if off else max(max_tokens, THINKING_MIN_TOKENS),
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],   # str, or [{"type": "text", "text", "cache_control"?}]
            "output_config": output_config,
        }
        if self.thinking is not None:
            kw["thinking"] = self.thinking
        if self.fallbacks:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    async def complete_json(self, system: str, user: str, schema: dict, timeout: float,
                            max_tokens: int = 400, usage_out: Optional[dict] = None) -> dict:
        kw = self.request(system, user, schema, max_tokens)
        api = self.client.with_options(timeout=timeout)
        create = api.beta.messages.create if self.fallbacks else api.messages.create
        try:      # the SDK timeout is per phase (connect/read...); wait_for makes it a hard deadline
            r = await asyncio.wait_for(create(**kw), timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise _anthropic_error(e) from e
        self._note(getattr(r, "usage", None), usage_out)
        stop = getattr(r, "stop_reason", None)
        if stop == "refusal":     # schema not guaranteed; Haiku 5.5 has no server-side fallback
            details = getattr(r, "stop_details", None)
            raise LLMRefusal(f"refusal (category={getattr(details, 'category', None)})")
        if stop != "end_turn":
            raise LLMInvalid(f"stop_reason={stop}: structured output not guaranteed")
        text = next((b.text for b in (getattr(r, "content", None) or []) if getattr(b, "type", "") == "text"), None)
        if text is None:
            raise LLMInvalid("no text block in response")
        return parse_json_text(text)

    def _note(self, u: Any, usage_out: Optional[dict] = None) -> None:
        d = {"calls": 1}
        if u is not None:
            read = _num(getattr(u, "cache_read_input_tokens", 0))
            d.update(cache_hits=int(read > 0), cache_read_tokens=read,
                     cache_write_tokens=_num(getattr(u, "cache_creation_input_tokens", 0)),
                     input_tokens=_num(getattr(u, "input_tokens", 0)), output_tokens=_num(getattr(u, "output_tokens", 0)))
        _tally(d, self.usage, usage_out)


def _anthropic_error(e: Exception) -> LLMError:
    if isinstance(e, LLMError):
        return e
    try:
        import anthropic
    except ImportError:            # a fake client in tests without the SDK
        anthropic = None
    if anthropic is not None:
        if isinstance(e, anthropic.APITimeoutError):          # subclass of APIConnectionError: check first
            return LLMTimeout(f"anthropic timeout: {e}")
        if isinstance(e, anthropic.RateLimitError):
            return LLMError(f"rate limited (429): {getattr(e, 'message', e)}")
        if isinstance(e, anthropic.APIStatusError):
            return LLMError(f"HTTP {e.status_code}: {getattr(e, 'message', e)}")
        if isinstance(e, anthropic.APIConnectionError):
            return LLMError(f"connection error: {e}")
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return LLMTimeout(str(e) or "timeout")
    return LLMError(f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- OpenAI / ChatGPT-compatible
OPENAI_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-6-luna"     # OpenAI's "most efficient model for focused, high-volume tasks"
# OpenAI reasoning models whose docs say they reject reasoning_effort "none" (use "low" instead).
NO_EFFORT_NONE = ("gpt-6-astra", "gpt-6.1-sol", "o1", "o3", "o4")
_REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def default_effort(model: str, official: bool) -> Optional[str]:
    """reasoning_effort for the lowest latency, or None to omit it (non-reasoning or non-OpenAI models).
    LLM_REASONING_EFFORT=none|minimal|low|medium|high|omit overrides."""
    forced = str(os.environ.get("LLM_REASONING_EFFORT", "") or "").strip().lower()
    if forced == "omit":
        return None
    if forced:
        return forced
    if not official or not model.startswith(_REASONING_PREFIXES):
        return None
    return "low" if model.startswith(NO_EFFORT_NONE) else "none"


class OpenAICompatLLM:
    """OpenAI Chat Completions (ChatGPT models) or any compatible server: Azure/OpenRouter/Groq/vLLM/Ollama/...

    Defaults follow OpenAI's API reference: max_completion_tokens (max_tokens is deprecated), structured
    outputs via response_format json_schema strict, reasoning_effort for GPT-5/6 reasoning models, no sampling
    params. Compatible servers differ, so a 400 that names a parameter flips that one setting and retries
    once; the client remembers, so only the first call (normally the warm-up) pays for it.
    """

    def __init__(self, base_url: str, api_key: str, model: str, client: Any = None):
        import httpx
        base = base_url.rstrip("/")
        self.official = base == OPENAI_URL
        self.url = base + "/chat/completions"
        self.client = client or httpx.AsyncClient()
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model = model
        self.name = f"openai:{model}"
        self.max_tokens_field = os.environ.get("LLM_MAX_TOKENS_FIELD") or (
            "max_completion_tokens" if self.official else "max_tokens")
        self.effort = default_effort(model, self.official)
        self.format = "json_schema"           # -> "json_object" if the server lacks structured outputs
        self.usage = _new_usage()

    def reasons(self) -> bool:
        """True if the model may spend output tokens reasoning before the JSON (effort set, or omitted on a
        reasoning model, e.g. openai/gpt-5-mini on OpenRouter, so the server default applies)."""
        if self.effort == "none":
            return False
        return self.effort is not None or self.model.split("/")[-1].startswith(_REASONING_PREFIXES)

    def settings(self) -> dict:
        return {"effort": self.effort, "max_tokens_field": self.max_tokens_field, "format": self.format}

    def request(self, system: str, user: str, schema: dict, max_tokens: int = 400) -> dict:
        schema = strict_schema(schema)
        if self.format == "json_schema":
            fmt = {"type": "json_schema", "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema}}
        else:                                 # JSON mode: the schema travels in the prompt instead
            fmt = {"type": "json_object"}
            system = f"{system}\n\nReply with one JSON object matching this JSON Schema:\n{json.dumps(schema)}"
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # reasoning tokens count toward this cap, like Claude's thinking (same floor)
            self.max_tokens_field: max(max_tokens, THINKING_MIN_TOKENS) if self.reasons() else max_tokens,
            "response_format": fmt,
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
        return body

    def _fix(self, status: int, text: str, sent: dict) -> Optional[tuple[str, Any]]:
        """(setting, new value) for a 400 complaining about a parameter we `sent`, else None."""
        if status != 400:
            return None
        t = text.lower()
        if "context length" in t or "too large" in t:          # prompt too long: no parameter flip helps
            return None
        if "reasoning_effort" in t and sent["effort"]:
            low = sent["effort"] == "none" and self.official and "unsupported parameter" not in t
            return "effort", ("low" if low else None)          # bad value -> "low"; unknown parameter -> omit
        if "max_completion_tokens" in t or ("max_tokens" in t and "unsupported" in t):
            return "max_tokens_field", ("max_tokens" if sent["max_tokens_field"] == "max_completion_tokens"
                                        else "max_completion_tokens")
        if ("json_schema" in t or "response_format" in t) and sent["format"] == "json_schema":
            return "format", "json_object"
        return None

    async def complete_json(self, system: str, user: str, schema: dict, timeout: float,
                            max_tokens: int = 400, usage_out: Optional[dict] = None) -> dict:
        import httpx
        deadline = time.monotonic() + timeout
        for attempt in (0, 1):
            sent = self.settings()
            left = max(0.05, deadline - time.monotonic())
            try:              # httpx timeouts are per phase; wait_for makes `timeout` a hard total deadline
                resp = await asyncio.wait_for(self.client.post(
                    self.url, json=self.request(system, user, schema, max_tokens), headers=self.headers,
                    timeout=left), left)
            except (httpx.TimeoutException, asyncio.TimeoutError) as e:
                raise LLMTimeout(f"openai-compatible timeout: {e!r}") from e
            except httpx.HTTPError as e:
                raise LLMError(f"connection error: {e!r}") from e
            if resp.status_code < 400:
                break
            fix = self._fix(resp.status_code, resp.text, sent) if attempt == 0 else None
            if fix and deadline - time.monotonic() > 0.1:
                name, value = fix
                if getattr(self, name) == sent[name]:          # not already adapted by a concurrent call
                    setattr(self, name, value)
                    log.warning("%s rejected %s; adapted (effort=%s, tokens=%s, format=%s) and retrying",
                                self.name, name, self.effort, self.max_tokens_field, self.format)
                continue
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMInvalid(f"unexpected response shape: {resp.text[:120]!r}") from e
        self._note(data.get("usage") if isinstance(data, dict) else None, usage_out)
        finish = choice.get("finish_reason")
        if msg.get("refusal") or finish == "content_filter":
            raise LLMRefusal(f"refusal: {msg.get('refusal') or finish}")
        if finish not in (None, "stop"):
            raise LLMInvalid(f"finish_reason={finish}: output may be truncated")
        return parse_json_text(msg.get("content"))

    def _note(self, u: Any, usage_out: Optional[dict] = None) -> None:
        d = {"calls": 1}
        if isinstance(u, dict):
            cached = _num((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
            d.update(cache_hits=int(cached > 0), cache_read_tokens=cached,
                     input_tokens=_num(u.get("prompt_tokens", 0)), output_tokens=_num(u.get("completion_tokens", 0)))
        _tally(d, self.usage, usage_out)


# ----------------------------------------------------------------------------- factory
OPENAI_ALIASES = ("openai", "chatgpt", "gpt", "openai_compat", "compat")
ANTHROPIC_ALIASES = ("anthropic", "claude")


def make_llm() -> Optional[LLM]:
    """Build the configured client (config.LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL + API key env).
    LLM_PROVIDER=auto (default): Claude if Anthropic credentials exist, else ChatGPT/OpenAI if OPENAI_API_KEY
    (or LLM_BASE_URL) is set, else None; ChatGPT first when a GPT model was chosen (OPENAI_MODEL set, or
    LLM_MODEL not claude-*). Logs the choice once at INFO. None when no provider is usable -> heuristic."""
    provider = str(getattr(config, "LLM_PROVIDER", "auto") or "").strip().lower()
    if provider in ("", "heuristic", "none", "off"):
        log.info("LLM: none (LLM_PROVIDER=%r); heuristic claim extraction", provider)
        return None
    try:
        if provider in ANTHROPIC_ALIASES or provider in OPENAI_ALIASES:
            llm = (_make_anthropic if provider in ANTHROPIC_ALIASES else _make_openai)()
            return _chosen(llm, f"LLM_PROVIDER={provider}")
        if provider == "auto":
            want_gpt = _gpt_requested()
            order = (_make_openai, _make_anthropic) if want_gpt else (_make_anthropic, _make_openai)
            llm = order[0](quiet=True) or order[1](quiet=True)
            if llm is None:
                return _none_found()
            if want_gpt:
                why = f"auto: GPT model requested ({want_gpt})" + (
                    "" if isinstance(llm, OpenAICompatLLM) else " but no OPENAI_API_KEY / LLM_BASE_URL; using Claude")
            else:
                why = "auto: Anthropic credentials found" if isinstance(llm, AnthropicLLM) else \
                    "auto: no Anthropic credentials; OPENAI_API_KEY / LLM_BASE_URL set"
            return _chosen(llm, why)
    except Exception:
        log.exception("LLM provider %r failed to initialise; heuristic claim extraction only", provider)
        return None
    log.warning("unknown LLM_PROVIDER %r; heuristic claim extraction only", provider)
    return None


def _gpt_requested() -> str:
    """Which setting asks for a GPT model ("" if none): OPENAI_MODEL, or an LLM_MODEL that isn't Claude's."""
    if os.environ.get("OPENAI_MODEL"):
        return f"OPENAI_MODEL={os.environ['OPENAI_MODEL']}"
    model = str(getattr(config, "LLM_MODEL", "") or "").strip()
    return f"LLM_MODEL={model}" if model and not model.lower().startswith("claude") else ""


def _chosen(llm: Optional[LLM], why: str) -> Optional[LLM]:
    if llm is not None:
        log.info("LLM: %s (%s)", llm.name, why)
    return llm


def _none_found() -> None:
    log.warning("no LLM credentials (ANTHROPIC_API_KEY or OPENAI_API_KEY); heuristic claim extraction only")
    return None


def _make_anthropic(quiet: bool = False) -> Optional[AnthropicLLM]:
    try:
        import anthropic
    except ImportError:
        if not quiet:
            log.warning("anthropic SDK not installed; heuristic claim extraction only")
        return None
    try:
        # The SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, `ant auth login` profiles and WIF here; with
        # none of them it may raise (CredentialsError) instead of returning a key-less client.
        client = anthropic.AsyncAnthropic(max_retries=0)
        has_creds = bool(client.api_key or getattr(client, "auth_token", None) or getattr(client, "credentials", None))
    except Exception as e:
        client, has_creds = None, False
        log.debug("anthropic client not constructible: %r", e)
    if not has_creds:
        if not quiet:
            log.warning("no Anthropic credentials (ANTHROPIC_API_KEY or `ant auth login`); heuristic extraction only")
        return None
    # LLM_MODEL is the one knob (validated by _claude_model). ANTHROPIC_MODEL is deliberately ignored: it is Claude
    # Code's own setting, so it may be exported in a teammate's shell with an alias ("opus") or a "[1m]" suffix.
    return AnthropicLLM(client)


def _make_openai(quiet: bool = False) -> Optional[OpenAICompatLLM]:
    base = str(getattr(config, "LLM_BASE_URL", "") or "").strip()
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    if not key and not base:       # a local server (LLM_BASE_URL) may not need a key
        if not quiet:
            log.warning("LLM_PROVIDER=openai but no OPENAI_API_KEY / LLM_BASE_URL; heuristic claim extraction only")
        return None
    official = base.rstrip("/") in ("", OPENAI_URL)
    model = os.environ.get("OPENAI_MODEL") or str(config.LLM_MODEL)
    if model.startswith("claude-"):              # LLM_MODEL still holds the Claude default
        if official:
            model = OPENAI_DEFAULT_MODEL
        else:
            log.warning("LLM_BASE_URL set but LLM_MODEL=%s: set OPENAI_MODEL to a model your endpoint serves", model)
    return OpenAICompatLLM(base or OPENAI_URL, key, model)
