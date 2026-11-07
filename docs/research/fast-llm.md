# Which fast LLM can meet the claim and verdict latency targets?

Ticket: issue 7. Researched Sat 7 Nov 2026. Targets: claim extraction under 500 ms per sentence, verdicts in 1-2 s, reliable structured output.

## Answer

**Use Cerebras `gpt-oss-120b` for both claims and verdicts as the default, with separate settings for each. Test GPT-6.1 Terra as the verdict challenger.**

| Stage | Default | Settings | Fallback |
|---|---|---|---|
| Claims | Cerebras `gpt-oss-120b` | `reasoning_effort: low`, `reasoning_format: hidden`, strict `json_schema` | Cerebras `qwen-3.8-27b` with `reasoning_effort: none` |
| Verdicts | Cerebras `gpt-oss-120b` | `reasoning_effort: medium`, hidden reasoning, strict `json_schema` | OpenAI `gpt-6.1-terra` with reasoning effort `none` |

**Same model?** Start with the same model and provider, but keep the model a per-stage config value. Claims and verdicts have different needs: claims need raw speed, while verdicts have a larger budget that should go to accuracy. Move verdicts to Terra if it scores better on the team's 20-claim eval and its p95 stays under 2 s.

**Confidence: medium.** Cerebras is the best-supported choice, but **no published benchmark confirms that any model meets the 500 ms target on this workload**. The kickoff test below must settle that. The biggest unknown is verdict quality, because `gpt-oss-120b` has no Intelligence Index score on the pages checked.

**Cerebras needs the paid Developer tier.** The Free Trial allows 5 requests per minute, which a live debate would exceed.

## Why published numbers don't settle this

Artificial Analysis (AA) is the main independent source, but its figures can't be read directly against our targets:

- **Its prompts are far bigger than ours.** AA averages workloads of about 1k, 10k and 100k input tokens plus multi-turn agentic runs. A claim prompt is likely a few hundred tokens.
- **It measures from `us-central1`,** and its latency figures include network time. We will be calling from London.
- **Its speed figure includes waiting time.** Output speed runs over the full request, including TTFT and reasoning tokens, so it probably understates raw generation speed.
- **It has no thinking-off result for Claude Haiku 5.5.** That is the setting we would actually use.

Only total time matters here. The pipeline needs the complete JSON before it can act, and the quote check needs the complete verdict. A fast first token doesn't help if the whole object arrives late.

## Comparison

"First answer" is AA's time to first answer token, averaged over its heavy workloads. Use it to rank models, not to predict our latency.

| Model (setting) | Provider | First answer, s (AA) | Intelligence Index (AA) | Speed, tok/s | Strict schema |
|---|---|---|---|---|---|
| `gpt-oss-120b` (variant unstated) | Cerebras | 2.07 | none shown | ~3000 (vendor) | Yes (vendor) |
| `gpt-oss-120b` (variant unstated) | Groq | 4.79 | none shown | 500 (vendor) | Yes, no streaming (vendor) |
| `qwen-3.8-27b` (non-reasoning) | AA figure not Cerebras-specific | 5.30 | 10 | ~1850 on Cerebras (vendor) | Yes (vendor) |
| `gpt-oss-20b` (low) | Multi-provider figure | 3.75 | none shown | 1000 on Groq (vendor) | Yes, no streaming (vendor) |
| GPT-6 Luna (non-reasoning) | OpenAI | 1.03 | 9 | not found for this setting | Not confirmed for Luna |
| GPT-6.1 Terra (non-reasoning) | OpenAI | 1.75 | 31 | not found for this setting | Yes (vendor) |
| Gemini 3.8 Flash (low) | Google | 2.83 | 23 | 113 at high (AA) | Constrained, but docs say validate |
| Gemini 3.5 Flash-Lite (reasoning) | Google | 5.09 | 12 | 294 (AA) | Constrained, but docs say validate |
| Claude Haiku 5.5 (low, thinking on) | Anthropic | 9.17 | 19 | 235 at max (AA) | Yes, with exceptions (vendor) |
| Mercury 2.5 | Inception | 2.27 | none shown | not checked | JSON mode (AA) |

### Estimated generation time

This is my own arithmetic: tokens divided by the speeds above. It excludes network time, queueing and TTFT, and the token counts are assumptions. Treat the results as rough.

| Speed source | 80 tokens (claim) | 200 tokens (verdict) | 500 tokens (verdict plus reasoning) |
|---|---|---|---|
| Cerebras `gpt-oss-120b`, ~3000 (vendor) | 27 ms | 67 ms | 167 ms |
| Cerebras `qwen-3.8-27b`, ~1850 (vendor) | 43 ms | 108 ms | 270 ms |
| Groq `gpt-oss-20b`, 1000 (vendor) | 80 ms | 200 ms | 500 ms |
| Groq `gpt-oss-120b`, 500 (vendor) | 160 ms | 400 ms | 1000 ms |
| Gemini 3.5 Flash-Lite, 294 (AA) | 272 ms | 680 ms | 1701 ms |
| Claude Haiku 5.5, 235 (AA, max effort) | 340 ms | 851 ms | 2128 ms |

Two conclusions follow:

- **Claims:** at 235-294 tok/s, generation alone uses more than half of the 500 ms budget, before any network time or TTFT. Only the vendor-claimed speeds on Cerebras and Groq leave comfortable room.
- **Verdicts:** if the Cerebras speed holds, 500 tokens takes under 200 ms, which leaves room for reasoning. Slower models have to skip reasoning to stay within 1-2 s.

## Why not the others

- **Groq:** the closest alternative, but AA measures it behind Cerebras on `gpt-oss-120b`. Its docs say structured outputs don't support streaming, and the base Developer limit is 30 requests per minute and 8K tokens per minute. That could throttle parallel checks. Check your account's actual limits if you use it.
- **GPT-6.1 Terra:** the best quality among low-latency settings, and the OpenAI rate limits are generous. It is the verdict challenger, but its speed at effort `none` isn't published, so 1-2 s is unconfirmed.
- **GPT-6 Luna:** the lowest measured first-answer time among models with a score, but its index of 9 is weak.
- **Claude Haiku 5.5:** thinking can be disabled at `low`, `medium` and `high` effort, which is the configuration worth testing. Anthropic warns that reasoning-like text appears in replies more often with thinking off or at `low` effort. Its speed looks tight for claims but plausible for verdicts.
- **Gemini:** Flash-Lite and 3.8 Flash have no listed "off" thinking level, and Flash-Lite scores low. Google's docs also say to validate values in your application.
- **Mercury:** the only structured-output evidence found is secondary. OpenRouter also marks its Mercury 2 listing as going away on 8 Nov 2026, which is judging day. I did not reach Inception's own docs.

## Structured output reliability

| Provider | Stated behaviour | Handle in code |
|---|---|---|
| Cerebras | With `strict: true`, output is guaranteed to conform to the supported schema subset. | No `oneOf`, `allOf`, `pattern`, `format`, `minItems` or `maxItems`. Don't combine `tools` with `response_format` unless the model's docs allow it. Reasoning tokens count toward `max_completion_tokens`, so don't set it too low. |
| Groq | Strict mode "will always match your schema exactly". | No streaming or tool use with structured outputs. Preview models may be discontinued at short notice. |
| OpenAI | Output adheres to the schema, except on refusals or incomplete responses. | First request with a new schema is slower, so send a warm-up request before the demo. |
| Anthropic | Constrained decoding, except on refusals and `max_tokens` truncation. | Enum casing can vary. First request with a new schema compiles a grammar, cached for 24 hours from last use. Check `stop_reason` before parsing. |
| Google | Constrained generation, with no explicit guarantee. | Some constraints are silently stripped, including `minLength` and `maxLength`. |

Whichever model is used, validate against `schemas.py` and keep the quote and numeric checks. Schema conformance says nothing about whether a verdict is correct.

## Kickoff test (about 20 minutes)

Run this from the venue network before freezing the choice. No provider keys were available during this research, so none of it has been run.

1. **Claims:** send 30 real sentences from the 3-minute test debate to `gpt-oss-120b` (low) and `qwen-3.8-27b` (none), with the real `Claim` schema. Record total time p50 and p95 over a reused connection.
2. **Verdicts:** send the 20-claim eval set with realistic retrieved evidence to `gpt-oss-120b` (medium), `gpt-6.1-terra` (none) and `claude-haiku-5-5` (low, thinking disabled). Record p50, p95, accuracy, and how many quotes pass the check.
3. **Concurrency:** repeat with 5 parallel requests to expose queueing and rate limits.

**Decide with these rules:**

- Keep a claims model only if its p95 is under 500 ms. If none passes, shorten the prompt and schema first, then try Groq.
- Pick the most accurate verdict model with p95 under 2 s and no fabricated quotes getting through.

## What I couldn't verify

- **Actual latency on this workload.** I found no short-prompt or London-based measurements for any provider, and I ran no live tests.
- **Vendor speed claims.** Cerebras's ~3000 and ~1850 tok/s and Groq's speed figures come from their own docs. I couldn't find independent AA output speeds for those provider and model pairs.
- **`gpt-oss-120b` quality.** AA shows no Intelligence Index for it. The index is also a general measure, not a fact-checking score.
- **Which variant AA's provider figures use.** The `gpt-oss-120b` provider page doesn't say. It also has FAQ figures that differ slightly from its table (Cerebras 2.26 s vs 2.07 s, Groq 4.90 s vs 4.79 s).
- **Figures for non-reasoning models on fast hardware.** AA's Qwen non-reasoning figure isn't Cerebras-specific, and AA's main Qwen provider table doesn't list Cerebras or Groq.
- **Cerebras streaming with structured outputs.** The docs I read don't say. It isn't needed for this design, since we wait for complete JSON.
- **Cerebras regions and London round-trip time.**
- **Exact AA snapshot.** AA figures are five-day averages read on 7 Nov 2026, with no exact date on the page. They were read through a page-extraction tool and will change, so re-check them if the decision depends on a small difference.

## Sources

Independent:

- [AA methodology](https://artificialanalysis.ai/methodology/performance-benchmarking)
- [AA leaderboard](https://artificialanalysis.ai/leaderboards/models)
- [AA `gpt-oss-120b` providers](https://artificialanalysis.ai/models/gpt-oss-120b/providers)
- [AA Qwen3.8 27B providers](https://artificialanalysis.ai/models/qwen3-8-27b/providers)
- [AA Claude Haiku 5.5](https://artificialanalysis.ai/models/claude-haiku-5-5)
- [AA Gemini 3.5 Flash-Lite](https://artificialanalysis.ai/models/gemini-3-5-flash-lite)
- [AA Gemini 3.8 Flash](https://artificialanalysis.ai/models/gemini-3-8-flash)

Vendor:

- Cerebras: [models](https://inference-docs.cerebras.ai/models/overview), [structured outputs](https://inference-docs.cerebras.ai/capabilities/structured-outputs), [reasoning](https://inference-docs.cerebras.ai/capabilities/reasoning), [rate limits](https://inference-docs.cerebras.ai/support/rate-limits), [streaming](https://inference-docs.cerebras.ai/capabilities/streaming)
- Groq: [models](https://console.groq.com/docs/models), [structured outputs](https://console.groq.com/docs/structured-outputs), [reasoning](https://console.groq.com/docs/reasoning), [rate limits](https://console.groq.com/docs/rate-limits)
- OpenAI: [models](https://developers.openai.com/api/docs/models), [GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- Anthropic: [models](https://platform.claude.com/docs/en/models/overview), [Haiku 5.5 prompting](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5), [structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [reducing latency](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-latency)
- Google: [models](https://aistudio.google.com/docs/models), [thinking](https://aistudio.google.com/docs/thinking), [structured output](https://aistudio.google.com/docs/structured-output)

Secondary, used only for Mercury:

- [OpenRouter Mercury 2 listing](https://openrouter.ai/inception/mercury-2)
