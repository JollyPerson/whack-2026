# Which streaming speech-to-text fits the latency budget?

Ticket: #4. Researched 2026-11-07. Target: final text in under 1.5 s, one stream per speaker.

## Answer

**Use Soniox `stt-rt-v6`, with Deepgram Nova-3 as the fallback behind the same adapter.**

- Every serious candidate clears 1.5 s in the benchmark, with P99 under 500 ms from end of speech. Latency does not separate them, so accuracy and behaviour during long speeches decide it.
- Soniox has the lowest error rate in the benchmark and a fast, tight latency tail. It also finalizes text as tokens, not only at end of turn, which suits long debate speeches.
- On the ticket's original question, AssemblyAI beats Deepgram on accuracy and Deepgram beats AssemblyAI on latency. Since both meet the budget, **AssemblyAI `universal-3-6-pro` is the better of those two for this project**. It is also statistically tied with Soniox on accuracy, so it is a reasonable swap if Soniox fails the bake-off.
- **Avoid Deepgram Flux here.** It is built for voice agents, has no independent benchmark and no documented diarization, and its transcript is only marked complete at `EndOfTurn` (see "Long speeches").

**Run a bake-off before committing.** Soniox v6 shipped on 3 Nov 2026 and has one independent benchmark run. Nothing here tests UK accents, debate speech or real keyterm lift. Stream the team's recorded test debate through Soniox and Deepgram, and compare numbers and topic words (about 30 minutes for role A).

## Independent measurements

Source: [Pipecat `stt-benchmark` README](https://github.com/pipecat-ai/stt-benchmark), last updated 2026-11-06. 1,000 samples of real human speech.

| Vendor / model | Pooled semantic WER | Perfect transcripts | TTFS median | TTFS P95 | TTFS P99 |
|---|---|---|---|---|---|
| **Soniox `stt-rt-v6`** | **0.71%** | 88.8% | 266 ms | 324 ms | 343 ms |
| AssemblyAI `universal-3-6-pro` | 0.80% | 88.7% | 307 ms | 401 ms | 498 ms |
| AssemblyAI `universal-3-5-pro` | 0.95% | 86.0% | 282 ms | 354 ms | 393 ms |
| Speechmatics `linden-1` | 0.96% | 83.9% | 369 ms | 438 ms | 690 ms |
| Cartesia `ink-2` | 1.02% | 83.6% | 299 ms | 328 ms | 1584 ms |
| **Deepgram `nova-3-general`** | 1.37% | 77.7% | **247 ms** | **298 ms** | **326 ms** |
| OpenAI `gpt-realtime-whisper` | 2.30% | 73.3% | 740 ms | 878 ms | 1080 ms |
| AssemblyAI `universal-streaming-english` | 2.42% | 66.8% | 256 ms | 362 ms | 417 ms |
| ElevenLabs `scribe_v2_realtime` | 3.13% | 80.3% | 281 ms | 348 ms | 407 ms |

This is a selection. Meta `muse-voice-transcribe-1.0` (0.73%) and xAI `grok-voice-transcribe-2.0` (0.79%) are as accurate, but their tails are much slower, at 1922 ms and 2148 ms P99.

**How to read these numbers**

- TTFS is measured from end of speech to the final segment, and it includes the VAD's stop wait. The [February write-up](https://www.daily.co/blog/benchmarking-stt-for-voice-agents/) used a fixed 200 ms stop threshold.
- **The numbers assume finalization forced by client-side VAD.** Pipecat sends a finalize message when its VAD detects a stop ([Deepgram service](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/services/deepgram/stt.py), [Soniox service](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/services/soniox/stt.py)), and the [benchmark config](https://github.com/pipecat-ai/stt-benchmark/blob/main/src/stt_benchmark/services.py) sets `vad_force_turn_endpoint=True` for AssemblyAI. Vendor endpointing alone will not necessarily match them.
- **Soniox and AssemblyAI 3.6 are not separable on accuracy.** The README says gaps under about 0.25 points among the top services fall within the 95% confidence interval. Deepgram's gap to Soniox (0.66 points) exceeds that margin.
- Wrong numbers, names and negations count as errors, but format differences such as "3" and "three" do not. There is no separate score for numbers.

**How independent it is:** Daily maintains Pipecat and publishes the benchmark, and its blog lists Deepgram as a partner and says the authors "worked with STT vendors" on configurations. The code and method are public. It is the best evidence found, but it is not free of commercial ties.

**Limits that affect us**

- English only, one dataset, and a single US network location using US endpoints (per the February write-up; not re-confirmed for the latest rows).
- Each row is a separate point-in-time run. No keyterms are configured for the rows checked (Soniox, Deepgram, AssemblyAI 3.6).
- Flux was excluded because its turn detection cannot be disabled.

## Vendor claims (not independently verified)

| Vendor | Claim | Source |
|---|---|---|
| Deepgram Nova-3 | "sub-300 ms" streaming latency; total client-side transcript latency 200–500 ms | [Measuring latency](https://developers.deepgram.com/docs/measuring-streaming-latency.mdx) |
| Deepgram Flux | End-of-turn detection about 260 ms at defaults, range 100–500 ms | [Flux quickstart](https://developers.deepgram.com/docs/flux/quickstart), [Measuring latency](https://developers.deepgram.com/docs/measuring-streaming-latency.mdx) |
| AssemblyAI | First partial at about 750–800 ms by default; `interruption_delay=0` for the fastest first token | [Best practices](https://www.assemblyai.com/docs/voice-agents/best-practices) |
| Soniox | Non-final tokens "appear instantly"; no millisecond figure published in the pages read | [Real-time transcription](https://docs.soniox.com/stt/rt/real-time-transcription) |

## Feature comparison (from vendor docs)

| | Soniox `stt-rt-v6` | Deepgram Nova-3 | AssemblyAI `universal-3-6-pro` / 3.5 | Deepgram Flux |
|---|---|---|---|---|
| Interim and final | Per-token `is_final`; final tokens never change | `is_final` per segment, `speech_final` at pause | `Turn` messages; `end_of_turn: true` completes the turn | `TurnInfo` updates; `EndOfTurn` completes the turn |
| Finals during long speech | Yes by design; pace undocumented | Yes: "multiple `is_final: true`" before `speech_final` | Unclear; docs examples only show final words at turn end | Not documented (see below) |
| Pause / end of turn | Semantic: `<end>` token, tunable. Default `max_endpoint_delay_ms` is 2000 | `endpointing` ms, `speech_final`, `UtteranceEnd` | `min_turn_silence` / `max_turn_silence`, punctuation-based | `eot_threshold`, `eot_timeout_ms`, eager mode |
| Force finalize | `{"type": "finalize"}`, then `<fin>` | `Finalize` | `ForceEndpoint` | `ForceEndTurn` |
| Keyterms | `context.terms`; 8,000 tokens total context | Up to 100 terms and 500 tokens; 20–50 recommended | Up to 100 terms, 50 characters each | Same as Nova-3 |
| Update terms mid-stream | **No**: set at session start | Yes (`Configure`) | Yes (`UpdateConfiguration`) | Yes (`Configure`) |
| Concurrent streams | **10 by default**, raisable in Console | 150 Pay As You Go; 225 or 150 Growth by region | No hard cap; 200 new streams per minute paid, 5 free | 150 Pay As You Go, same pool as above |
| Python SDK | `soniox` 2.11.0 (3 Nov 2026), sync and async | `deepgram-sdk` 7.17.0 (4 Nov 2026), async | `assemblyai` 1.8.2 (23 Oct 2026) | `deepgram-sdk`, `listen.v2` |
| Streaming diarization | Yes, up to 15 speakers. Early finalizing "significantly" hurts accuracy | Yes, `diarize_model=latest` (v1); `speaker` per word | Yes, `speaker_labels`, `max_speakers` 1–10 | Not in the API spec |

Sources:
[Soniox endpointing](https://docs.soniox.com/stt/rt/endpoint-detection),
[finalization](https://docs.soniox.com/stt/rt/manual-finalization),
[context](https://docs.soniox.com/stt/concepts/context),
[diarization](https://docs.soniox.com/stt/concepts/speaker-diarization),
[limits](https://docs.soniox.com/stt/rt/limits-and-quotas),
[models](https://docs.soniox.com/stt/models),
[Python realtime client](https://docs.soniox.com/sdk/python-SDK/Full-SDK-reference/realtime_client);
[Deepgram interim and endpointing](https://developers.deepgram.com/docs/understand-endpointing-interim-results),
[keyterms](https://developers.deepgram.com/docs/keyterm),
[diarization](https://developers.deepgram.com/docs/diarization),
[rate limits](https://developers.deepgram.com/reference/api-rate-limits),
[Flux API](https://developers.deepgram.com/reference/speech-to-text/listen-flux);
[AssemblyAI WebSocket spec](https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket),
[3.5 Pro guide](https://www.assemblyai.com/docs/voice-agents/universal-3-5-pro-streaming-api),
[message sequence](https://www.assemblyai.com/docs/streaming/message-sequence);
PyPI for SDK versions.

## Why this matters for a debate

**Long speeches are the main risk, and no benchmark covers them.** TTFS measures end of speech on benchmark samples. A debater may talk for 30 s or more with only short pauses, and the fact-checker needs finals during that speech.

- **Soniox** and **Nova-3** both document finals before the speaker stops, but neither documents how often they arrive.
- **AssemblyAI** is less clear. Its 3.5 guide calls the model "optimized for real-time audio under 10 seconds", and the message-sequence page does not say whether words finalize mid-turn. A turn can end on a short pause with terminal punctuation, which may help. `ForceEndpoint` is the backstop.
- **Flux** has no interim/final distinction in the API spec beyond `TurnInfo` events, so a claim might wait for `EndOfTurn`. This is my reading of the spec, not a vendor statement. `ForceEndTurn` could work around it, but that discards the model's main selling point.

**Default settings can break the budget:**

- Soniox `max_endpoint_delay_ms` defaults to 2000 ms, and its suggested low-latency preset uses 1500 ms, which is the whole budget.
- AssemblyAI `max_turn_silence` defaults to 1536 ms in the WebSocket spec and 1000 ms in the 3.5 guide. These pages disagree, so set it explicitly.

## Recommended setup for role A

These settings are starting points from the docs and my judgement. None has been tested.

1. **One WebSocket per speaker**, 16 kHz mono PCM, using the async client. Set `model: stt-rt-v6` and `language_hints: ["en"]`. Two to five streams fit within the default limit of 10.
2. **Use token finals, not only turn ends.** Map final tokens to `Utterance(final=true)` at sentence punctuation, `<end>` or `<fin>`. Show non-final tokens as interim text.
3. **Finalize with Silero VAD**, sending `finalize` about 200 ms after end of speech, as Soniox advises. Do not send it more than once every few seconds, because Soniox warns that sending too often may cause disconnects. Keep endpoint detection on as a backstop with `max_endpoint_delay_ms` at about 1000.
4. **Keep segment finalization separate from `Pause`.** The Director needs a longer silence than transcript finalization does. Derive `Pause` from your own VAD threshold and tune it on the test debate.
5. **Send the context after pre-research and before the debate starts.** Include the motion in `context.general` or `context.text`, and names and topic words in `context.terms`. Because context is fixed for the session, open the stream after pre-research finishes.
6. **Do not rely on keyterms for numbers.** No vendor doc read here says priming improves numeric accuracy. Prime topic words, and have the numeric checker normalize spoken and digit forms in code. Check number formatting in the bake-off.
7. **Send keepalives during silence** with `keep_alive()`, so idle speakers do not time out.
8. **Hide the provider behind one adapter** that emits `Utterance` and `Pause`, so Deepgram can replace Soniox in minutes.

**Diarization fallback (shared mic only).** Use Deepgram Nova-3 with `diarize_model=latest`. Soniox says endpoint detection and early finalization reduce diarization accuracy, which conflicts with this low-latency setup. Deepgram's docs state no such conflict, but they publish no accuracy figure either, and streaming results carry no `speaker_confidence`. Treat diarization as a last resort and keep per-speaker channels as the plan.

## Could not verify

- **No measured latency for the real path**: phone browser, venue Wi-Fi, then the vendor. All benchmark numbers are from US servers. This needs a live test from the venue.
- **EU or UK endpoint availability and latency** for Soniox and AssemblyAI. Deepgram's rate-limit page lists a Europe region, but I did not measure it.
- **UK accents, Parliament audio and debate speech**: not covered by any source read.
- **How often finals arrive during continuous speech** for any vendor.
- **Keyterm lift** on any vendor: no independent data found.
- **Streaming diarization accuracy** for any vendor: no independent data found.
- **Flux latency and accuracy**: vendor figures only. Coval reports a comparison, but I did not open its benchmark.
- **Artificial Analysis leaderboard**: figures are chart-only and could not be extracted, so it gives no second independent source.
- **`stt-rt-v6` stability**: released 3 Nov 2026, so there is little real-world evidence yet.
- **AssemblyAI `universal-3-6-pro` limits**: its 3.6-specific guide was not read, so turn and partial details above come from general and 3.5 pages.
- **Which vendors the team's credits cover**: check for a Soniox key early, and allow time for Console limit requests if needed.
- **Doc reads were through a summarizing fetch tool**, except the benchmark README and Pipecat source, which were read directly. Confirm exact parameter names against the docs when implementing.
