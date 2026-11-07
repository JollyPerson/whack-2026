"""Claim-extraction latency bench (design §3.4). Owner: B. Needs a key; without one it says so and exits 0.

  .venv/bin/python scripts/bench_llm.py                      # production config + Haiku adaptive-thinking
  .venv/bin/python scripts/bench_llm.py --model claude-sonnet-5-5 --concurrency 3 --limit 10
  LLM_PROVIDER=openai .venv/bin/python scripts/bench_llm.py --model gpt-5.4-mini   # --model = same provider

Per config: one warm-up call (compiles the schema, writes the cache; not counted), a sequential pass, then a
pass at --concurrency. Prints p50/p95 time-to-first-token and total, mean output tokens and cache-hit rate.
Requests are built by app.brain.llm.AnthropicLLM.request(), i.e. exactly what the brain sends.
LLM_PROVIDER=openai benches the OpenAI-compatible client instead (total latency only, no streaming; per-call
token counts come from complete_json(usage_out=...), so they stay right under concurrency).
Decision rule: production config p50 total <= 500 ms -> ship it.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.brain.claims import keep_sentence, split_sentences  # noqa: E402
from app.brain.llm import (CLAUDE_MODEL_ID, AnthropicLLM, LLMError, OpenAICompatLLM, _new_usage,  # noqa: E402
                           default_effort, make_llm)
from app.brain.prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT, render_user  # noqa: E402
from app.bus import load_jsonl  # noqa: E402

EXTRA = [   # held-out sentences (different motion register) so the bench isn't only the fixture
    "Unemployment is 4.2 percent, the lowest it has been in a decade.",
    "The IFS says this policy would cost the Treasury three billion pounds a year.",
    "You voted against exactly this measure in 2019.",
    "Honestly, I just think it's the fair thing to do.",
    "Crime has gone up every single year since 2015.",
    "Prices will rise by at least ten percent if this passes.",
    "That figure is from a survey of just two hundred people.",
    "Can I finish, please?",
    "Most economists agree that rent controls reduce the housing supply.",
    "In Berlin, rents fell after the cap was introduced, according to the city's own data.",
]


def sentences(limit: int) -> list[tuple[str, str]]:
    """(side, sentence) pairs from the fixture plus EXTRA."""
    out: list[tuple[str, str]] = []
    sides: dict[str, str] = {}
    for ev in load_jsonl(ROOT / "fixtures/fake_debate.jsonl"):
        if ev.event == "SessionStart":
            sides = {k: v.side for k, v in ev.speakers.items()}
        elif ev.event == "Utterance":
            out += [(sides.get(ev.speaker, "prop"), s) for s in split_sentences(ev.text) if keep_sentence(s)]
    out += [("prop" if i % 2 == 0 else "opp", s) for i, s in enumerate(EXTRA)]
    return out[:limit]


def pct(xs: list, q: float):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))] * 1000) if xs else None


async def one_anthropic(client, llm: AnthropicLLM, motion: str, side: str, sent: str) -> dict:
    kw = llm.request(SYSTEM_PROMPT, render_user(motion, [], [], side, sent), OUTPUT_SCHEMA, 400)
    api = client.with_options(timeout=10.0)
    stream = api.beta.messages.stream if llm.fallbacks else api.messages.stream
    t0, ttft = time.perf_counter(), None
    try:
        async with stream(**kw) as s:
            async for _ in s.text_stream:
                ttft = ttft or time.perf_counter() - t0
            msg = await s.get_final_message()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:120]}
    total = time.perf_counter() - t0
    text = next((b.text for b in msg.content if b.type == "text"), "")
    ok = msg.stop_reason == "end_turn"
    if ok:
        try:
            json.loads(text)
        except ValueError:
            ok = False
    return {"ttft": ttft, "total": total, "out": msg.usage.output_tokens, "stop": msg.stop_reason, "ok": ok,
            "cache_read": msg.usage.cache_read_input_tokens or 0}


async def one_generic(llm, motion: str, side: str, sent: str) -> dict:
    t0, use = time.perf_counter(), {}
    try:
        await llm.complete_json(SYSTEM_PROMPT, render_user(motion, [], [], side, sent), OUTPUT_SCHEMA, timeout=10.0,
                                usage_out=use)
    except LLMError as e:
        return {"error": f"{e.cause}: {e}"[:120]}
    return {"ttft": None, "total": time.perf_counter() - t0, "ok": True, "stop": "stop",
            "out": use.get("output_tokens", 0), "cache_read": use.get("cache_read_tokens", 0)}


def openai_configs(llm: OpenAICompatLLM, models: list[str]) -> list[tuple[str, OpenAICompatLLM]]:
    """The configured client plus one per --model: same endpoint, key and HTTP client, another model."""
    out = [(llm.name, llm)]
    for m in models:
        if m.startswith("claude-") and llm.official:
            print(f"bench_llm: skipping --model {m}: OpenAI doesn't serve Claude models (bench it with LLM_PROVIDER=claude)")
            continue
        v = copy.copy(llm)
        v.model, v.name, v.usage, v.effort = m, f"openai:{m}", _new_usage(), default_effort(m, llm.official)
        out.append((v.name, v))
    return out


async def bench(name: str, call, sents: list, motion: str, concurrency: int) -> None:
    await call(motion, *sents[0])                                   # warm-up: schema compile + cache write
    for label, conc in (("seq", 1), (f"x{concurrency}", concurrency)):
        sem = asyncio.Semaphore(conc)

        async def run(side: str, s: str) -> dict:
            async with sem:
                return await call(motion, side, s)

        rs = await asyncio.gather(*(run(side, s) for side, s in sents))
        good = [r for r in rs if "error" not in r]
        errs = [r["error"] for r in rs if "error" in r]
        stops = sorted({r["stop"] for r in good if r["stop"] not in ("end_turn", "stop")})
        print(f"{name:<22} {label:<4} n={len(rs):<3} ttft p50/p95={pct([r['ttft'] for r in good], .5)}/"
              f"{pct([r['ttft'] for r in good], .95)} ms  total p50/p95={pct([r['total'] for r in good], .5)}/"
              f"{pct([r['total'] for r in good], .95)} ms  out_tok={statistics.mean([r['out'] for r in good] or [0]):.0f}"
              f"  cache_hit={sum(r['cache_read'] > 0 for r in good) / max(len(good), 1):.0%}"
              f"  bad_json={sum(not r['ok'] for r in good)}  errors={len(errs)}"
              + (f"  stops={stops}" if stops else "") + (f"  first_error={errs[0]!r}" if errs else ""))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", default=[], help="extra model to bench on the same provider (repeatable)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit", type=int, default=30, help="max sentences")
    ap.add_argument("--motion", default="This house would ban cars from city centres")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("brain.llm").setLevel(logging.INFO)       # make_llm() logs which provider and why

    llm = make_llm()
    if llm is None:
        print(f"bench_llm: no usable LLM (LLM_PROVIDER={config.LLM_PROVIDER!r}; set ANTHROPIC_API_KEY, run "
              f"`ant auth login`, or configure LLM_PROVIDER=openai). Nothing to bench.")
        return 0
    sents = sentences(a.limit)
    print(f"{len(sents)} sentences, provider={llm.name}")
    if not isinstance(llm, AnthropicLLM):
        for name, cfg in openai_configs(llm, a.model):
            await bench(name, lambda m, side, s, cfg=cfg: one_generic(cfg, m, side, s), sents, a.motion, a.concurrency)
        return 0

    client = llm.client
    try:
        info = await client.models.retrieve(llm.model)
        print(f"model access ok: {info.id}")
        n = await client.messages.count_tokens(model=llm.model, system=[{"type": "text", "text": SYSTEM_PROMPT}],
                                               messages=[{"role": "user", "content": "x"}])
        print(f"system prompt + 1-token message = {n.input_tokens} input tokens (cache minimum is 512)")
    except Exception as e:
        print(f"preflight failed: {type(e).__name__}: {e}")
        return 0

    configs: list[tuple[str, AnthropicLLM]] = [(f"prod:{llm.model}", llm)]
    if llm.model == "claude-haiku-5-5":
        adaptive = AnthropicLLM(client=client, model="claude-haiku-5-5")
        adaptive.thinking = None                                     # adaptive (the model default), effort low
        configs.append(("haiku-adaptive-low", adaptive))
    for m in a.model:
        if CLAUDE_MODEL_ID.fullmatch(m):
            configs.append((f"{m}-low", AnthropicLLM(client=client, model=m)))
        else:
            print(f"bench_llm: skipping --model {m}: not a Claude model id (bench it with LLM_PROVIDER=openai)")
    for name, cfg in configs:
        await bench(name, lambda m, side, s, cfg=cfg: one_anthropic(client, cfg, m, side, s), sents, a.motion,
                    a.concurrency)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
