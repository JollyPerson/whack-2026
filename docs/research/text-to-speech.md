# Text-to-speech for the Moderator

Ticket: #5. Researched Sat 7 Nov 2026. We ran no audio tests: no number here was measured by us, and nobody has listened to these voices for this report.

## Answer

**Use ElevenLabs Eleven v4 Turbo (`eleven_v4_turbo`) over its Text to Dialogue WebSocket. Keep Cartesia Sonic 3.6 (`sonic-3.6`) as the drop-in fallback. Do not use OpenAI.**

- **Delay:** v4 Turbo had the better tail in the strongest independent benchmark: 192 ms mean and 234 ms p95, against 284 ms and 358 ms for Sonic 3.6.
- **Quality:** v4 Turbo ranks joint first on the largest human-preference leaderboard. Sonic 3.6 is close behind.
- **The call is close:** another independent benchmark has Sonic 3.6 faster, and Cartesia's documentation suits a dry referee better. Before building on either, spend 15 minutes on the bake-off below.

TTS is a small share of the delay. The agreed budget is about 2–4 s end to end, and these two differ by roughly 100 ms. Both are fast enough, so choose by how the voice sounds if the bake-off is close.

## Comparison

"Independent" means measured by a third party. It does not mean disinterested: Coval sells voice-agent testing.

| | ElevenLabs v4 Turbo | Cartesia Sonic 3.6 | OpenAI `gpt-4o-mini-tts` | ElevenLabs Flash v2.5 | Inworld Realtime TTS-2 |
|---|---|---|---|---|---|
| **Vendor claim** | ~100 ms median, server-side only | "under 90ms" | none found | ~100 ms median, server-side only | not checked |
| **Coval TTFA mean / p95** (independent, us-east-1, 30 days) | 192 / 234 ms | 284 / 358 ms | not listed | 205 ms / not retrieved | 156 / 198 ms |
| **tts-bench first speech** (independent, Eastern US, warm connection) | 180 ms | 125 ms | 862 ms | not retrieved | 98 ms |
| **Voice Arena TTFA** (independent) | not in rows retrieved | 340.5 ms | 811.5 ms | not retrieved | 168.5 ms |
| **Artificial Analysis Elo** (human preference, native voices) | 1335 (rank range 1–2) | 1297 (range 3–5) | not in top 59 retrieved | 1075 (range 55–61) | 1268 (range 7–11) |
| **Voice Arena Elo** (human preference, US English) | not in rows retrieved | 1058 (rank 4) | 924 (rank 23) | not retrieved | 1049 (rank 6) |
| **Streaming** | Text to Dialogue WebSocket | WebSocket, SSE, bytes | Chunked HTTP | TTS WebSocket, HTTP | not checked |
| **Status** | Current | Current | Shuts down 6 Jan 2027 | Current | Current |

### Why the latency sources disagree

- **Coval and tts-bench reverse the order of the two leaders.** Coval has v4 Turbo ahead; tts-bench has Sonic 3.6 ahead. Both count audible speech rather than first bytes, and I could not explain the gap. Coval re-measures daily over 2,800+ samples per model, so I weight it higher. tts-bench is a small project whose author I could not assess.
- **Most of Coval's gap is leading silence.** Sonic 3.6 streams 135 ms of silence before speech, against 60 ms for v4 Turbo. Coval's "network roundtrip" figures are 149 ms and 131 ms. These two parts sum to TTFA, so that bucket probably includes server time as well (my inference).
- **All the figures above are from the US or unstated locations.** Nothing was measured from London. ElevenLabs lists Netherlands as a serving region, and Cartesia routes `api.cartesia.ai` to the closest endpoint, but its dedicated UK and EU deployments are enterprise only.
- **Vendor numbers are not comparable with these.** ElevenLabs says its figures exclude network and application time and should not be compared with client-side TTFA. Its docs also quote 100–150 ms TTFB in Europe, but that is for Flash over WebSockets.

## Fit for a dry referee

Neither vendor documents a "dry" setting, so this needs listening.

**Cartesia has the more direct documentation for this persona:**
- It recommends stable, realistic voices over emotive ones for agents, and names `Archie` (en-GB male) and `Gemma` (en-GB female).
- `generation_config` sets speed (0.6–1.5) and emotion, and `neutral` and `calm` are among the best-supported emotions. Emotion control is in beta.
- `locale` sets the accent, and `normalization` sets the conventions for dates, times and numbers. That matters when reading out statistics.

**ElevenLabs has less direct control on the v4 family:**
- The v4 page lists Stability and Similarity settings, with higher Stability keeping delivery closer to a fixed baseline. Speed and Style sliders and SSML are not available. That page covers Eleven v4; I assume Turbo matches, but the docs do not say so.
- Audio tags work on v4 Turbo, but ElevenLabs says they are "not perfect yet". Avoid them for this persona.
- Choosing a measured British voice from the Voice Library is the main control. I did not shortlist specific voices.

**OpenAI** offers free-text delivery instructions such as tone and pacing, which would suit the persona. Its latency rules it out, and it is the weakest on quality here.

## Integration notes

**ElevenLabs v4 Turbo**
- Use `wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input`. The usual TTS `stream-input` endpoint does not take v3 or v4 models, and the docs name only Text to Dialogue for v4 Turbo.
- Send `flush: true` after each Interjection. Otherwise the server waits for roughly 40 characters and 8 words, which would delay short lines.
- The connection drops after 20 s without a client message, and Interjections are about 30 s apart. Send `keep_alive`, or the reconnect handshake lands on the next Interjection.
- Each open connection holds a dialogue session from a separate concurrency pool, so keep one socket per process.
- Register exactly one voice. v4 Turbo allows only one per connection, which suits a single Moderator.
- Use a default or instant-clone voice. ElevenLabs says Professional Voice Clones are slower.

**Cartesia Sonic 3.6**
- Use `wss://api.cartesia.ai/tts/websocket` with `sonic-3.6`, or pin `sonic-3.6-2026-08-27` for a stable demo. Its WebSocket output is raw PCM.
- Keep the socket open. Cartesia says a new HTTPS connection can cost about as much as the TTFB itself.

**Either vendor**
- Write out numbers in the Interjection text, for example "four point two per cent". Do this in the Director's prompt, and listen for mistakes in the bake-off.
- Put TTS behind one function, `speak(text) -> audio stream`, so the vendor is one line to change.
- Keep the player's buffer small. ElevenLabs notes that a 500 ms buffer is common, which would outweigh the entire gap between these vendors.

## 15-minute bake-off (owner: D)

Run this from the venue network, with the demo laptop.

1. Open one warm WebSocket per vendor.
2. Send the same five Interjections, including UK statistics and a named source, and repeat them 10 times.
3. Time from send to the first non-silent sample. The first non-zero byte will flatter Sonic 3.6, which streams more silence.
4. Have two teammates pick blind on the projector speakers. Try `Archie` and `Gemma` against the best one or two ElevenLabs British voices.
5. Keep v4 Turbo unless Sonic 3.6 sounds clearly better or is more than about 100 ms faster.

## Alternatives

- **Inworld Realtime TTS-2** was faster than v4 Turbo in both independent sources that measured the pair (Coval and tts-bench) and scores well on quality. About 40–80 ms is not noticeable in a 2–4 s pipeline. Coval also recorded a 4.9 s worst case against 741 ms for v4 Turbo, and I did not read Inworld's docs. It is a reasonable third candidate, not a clear winner.
- **Inworld TTS Flash 2** measures 67 ms on Coval, but is lower on quality at Elo 1217.
- **ElevenLabs Flash v2.5** has similar latency to v4 Turbo with far lower quality (Elo 1075), and its number normalisation is Enterprise-only on request. There is no reason to choose it over v4 Turbo.
- **OpenAI `gpt-realtime-2.1-mini`** is OpenAI's named replacement for its TTS models. It requires the Realtime API instead of `/v1/audio/speech`. I found no independent TTS latency figure for it, though the full `gpt-realtime-2.1` measured 663 ms on tts-bench.

## What I could not verify

- **London latency:** no source measures from the UK, and the bake-off is the only way to get this figure.
- **The voices themselves:** I listened to nothing. "Suits a dry referee" is inferred from documentation, and leaderboards rank general preference rather than this persona.
- **v4 Turbo controls:** whether Stability and Similarity behave the same as on Eleven v4, and whether it can be used over HTTP streaming. The docs only name the Text to Dialogue WebSocket.
- **The tts-bench and Coval disagreement:** I have no explanation for it. I also did not read tts-bench's full method.
- **Sonic 3.6's "under 90ms":** Cartesia's post gives no method.
- **Number reading:** the word-error rates (1.7% and 1.5% on Coval) are not specific to statistics. Whether `apply_text_normalization` works well on v4 Turbo is untested. The parameter is present on the dialogue WebSocket.
- **Plan limits:** I did not check whether the team's credits cover dialogue-session concurrency or Cartesia's rate limits.

## Sources

**Vendor documentation (vendor-claimed)**
- [ElevenLabs models](https://elevenlabs.io/docs/overview/models)
- [How ElevenLabs latency figures are measured](https://elevenlabs.io/docs/eleven-api/concepts/latency-methodology), updated 29 Oct 2026
- [ElevenLabs: understanding latency](https://elevenlabs.io/docs/eleven-api/concepts/latency)
- [ElevenLabs latency optimization](https://elevenlabs.io/docs/best-practices/latency-optimization)
- [ElevenLabs TTS vs Text to Dialogue WebSockets](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/tts-vs-ttd-websockets)
- [ElevenLabs realtime Text to Dialogue guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tdd)
- [Eleven v4](https://elevenlabs.io/docs/overview/capabilities/text-to-speech/eleven-v4)
- [ElevenLabs audio tags](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/how-do-audio-tags-work-with-eleven-v3-and-v4)
- [Cartesia Sonic 3.6](https://docs.cartesia.ai/build-with-cartesia/tts-models/latest)
- [Introducing Sonic-3.6](https://www.cartesia.ai/blog/sonic-3.6), source of the "under 90ms" claim
- [Cartesia: choosing a voice](https://docs.cartesia.ai/build-with-cartesia/capability-guides/choosing-a-voice)
- [Cartesia: volume, speed and emotion](https://docs.cartesia.ai/build-with-cartesia/capability-guides/volume-speed-emotion)
- [Cartesia: compare TTS endpoints](https://docs.cartesia.ai/use-the-api/compare-tts-endpoints)
- [Cartesia documentation index](https://docs.cartesia.ai/llms.txt), which links the regional deployment page
- [OpenAI text to speech](https://platform.openai.com/docs/guides/text-to-speech)
- [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations)

**Independent measurement (retrieved 7 Nov 2026)**
- [Coval time to first audio](https://benchmarks.coval.ai/benchmarks/time-to-first-audio), with model pages for [v4 Turbo](https://benchmarks.coval.ai/models/eleven_v4_turbo), [Sonic 3.6](https://benchmarks.coval.ai/models/sonic-3.6) and [Inworld TTS-2](https://benchmarks.coval.ai/models/inworld-tts-2)
- [Artificial Analysis TTS leaderboard](https://artificialanalysis.ai/text-to-speech/leaderboard/provider-voice)
- [Voice Arena US English leaderboard](https://voicearena.com/tts-leaderboard/us-english)
- [tts-bench hosted APIs](https://5uck1ess.github.io/tts-bench/hosted.html), measured 1–4 Nov 2026
