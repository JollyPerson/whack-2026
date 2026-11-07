# Role B — Brain + integration lead: execution plan

Merged from three designs and one adversarial review (contracts/bus, claims + argument map, Director).
Build specific first, generalise later. When unsure, stay silent. Never block a teammate.

## What B owns

| Piece | File(s) | Done when |
|---|---|---|
| Contracts | `app/schemas.py`, `docs/CONTRACTS.md` | Every producer's sample validates; changes are additive only |
| Bus, log, replay | `app/bus.py`, `app/main.py`, `run.sh` | `./run.sh mock` runs end to end from a fresh clone; every run is replayable |
| Mocks + fixture | `app/mocks/*`, `fixtures/fake_debate.jsonl` | A, C and D can each develop alone |
| Claim extraction | `app/brain/claims.py`, `llm.py`, `prompts.py` | p50 < 500 ms per sentence (Haiku 5.5, no thinking, effort low); heuristic fallback when there's no key or a timeout |
| Argument map | `app/brain/argmap.py` | Answered / conceded / unanswered / non-responsive are deterministic; scoreboard and "call" |
| Director | `app/brain/director.py`, `voice.py` | Speaks only at pauses, ≤ 1 per 30 s, always names its source, balanced, explains every decision |
| Wiring | `app/brain/run.py` | One subscriber; pause synthesis; echo and bleed guards; session reset; crash restore |

## Decisions made (to be confirmed with the team; all reversible)

- **Envelope.** Flat JSON with an `event` discriminator (`type` is taken by `Claim.type`).
- **Flexibility.** Extra fields and unknown event types pass through, so teammates never wait on B. We traded `extra="forbid"` for a one-time log note per unknown field.
- **Speaker ids.** Opaque strings; sides come only from `SessionStart`. Nothing hard-codes "A"/"B".
- **Claim fields.** `importance` is a float from 0 to 1 (LLM 1–3 → 0.33 / 0.67 / 1.0). `type` is one of statistic / comparison / causal / attribution / fact / prediction / value / other. `value` and `prediction` are never checkable.
- **Speaking signal.** "Agent is speaking" is `TTSState`, sent by the single `?audio=1` tab; `Control(agent_speaking)` is also accepted.
- **Non-responsive turns** appear on the dashboard only and are never voiced. Unanswered points are voiced only at importance ≥ 0.67.
- **LLM.** Anthropic Haiku 5.5 by default. Switch with `LLM_PROVIDER=openai` plus `LLM_BASE_URL`; with no key, the heuristic runs.

## Measured (17:30, real Claude API, fixture debate)

| | p50 / p95 per sentence | Claim F1 | Checkable F1 | Type acc. | Notes |
|---|---|---|---|---|---|
| Claude Haiku 5.5 (thinking off, effort low, structured output, cached prompt) | **~0.9 s / ~1.5 s** | 0.96 | 1.00 | 0.96 | 100% cache hits, 0 invalid JSON |
| Heuristic (no key) | ~0 ms | 1.00* | 1.00 | 1.00 | *tuned on this fixture, so flattering |

- **The 500 ms target is missed.** Time to first token alone is about 650 ms. End to end (STT ~1 s + claim ~0.9 s + verdict 1–2 s) still fits the handoff's 2–4 s budget. Pitch the time-to-chip honestly.
- `CLAIM_TIMEOUT_S` was raised from 1.5 to 2.5 s; at 1.5 s, about 5% of calls fell back to the heuristic.
- **Link scores look bad but aren't.** Precision is 0.11–0.17 against a gold set with only 4 links. Read by hand, most of the LLM's links are defensible. Relabel the gold links with C on the real fixture.
- **Stretch goal, overnight:** publish the heuristic claim instantly, then upsert the LLM version under the same id. Chips would appear at about 0 ms.

## Timeline (Sat 7 → Sun 8 Nov)

| Time | B does | Checkpoint |
|---|---|---|
| 16:30–17:00 | ✅ Schemas, bus, main, mocks, fixture, run scripts, docs. Post "schemas v1.0 frozen" | Teammates can import and run `./run.sh mock` |
| 17:00–18:30 | ✅ Brain modules (argmap, Director, claims) + tests. Bench Haiku once a key exists | **18:30** standalone on mocks |
| 18:30–19:10 | Label ~25 gold claims with C on the real 3-minute fixture; run `eval/eval_extract.py` | Baseline numbers |
| 19:10–20:00 | Swap mocks for C's verdicts and D's dashboard; fix integration bugs | **20:00** `./run.sh drive` E2E → `fixtures/golden_run.jsonl` |
| 20:00–21:30 | Live mics with A: tune the bleed/echo guards, pause timing, fragment assembly | **21:30** live E2E |
| 21:30–01:30 | Prompt iterations (≤3, keep only if F1 ↑ and p50 not ↓); tune the Director's aggressiveness on recorded logs; teach a backup integrator the run modes | Regression vs golden before sleeping |
| 01:45–05:45 | Sleep (backup integrator on call, revert-only) | |
| 06:00–09:00 | Regression run, fixes only, record the best rehearsal as `fixtures/golden_demo.jsonl` | **09:00** feature freeze |
| 09:00–11:30 | Rehearse ≥3× on the demo laptop; B operates F/M/aggr from `?admin=1` | |

## Cut order if behind (never cut the bottom line)

1. LLM rephrasing of interjections → 2. two-stage linking → 3. non-responsive detection →
4. voiced unanswered prompts → 5. eval extras → 6. argmap turn detail → 7. scoreboard "call" text.
**Never cut:** JSONL log + replay + golden run, heuristic fallback, the pause-gated interjection path, evidence on every voiced verdict, the echo guard, session reset.

## Open questions for the team (ask now)

1. **Mics.** Wired/USB lav mics on the demo laptop as the primary path, or phones over a tunnel? Who gets the mics tonight?
2. **Keys.** Separate API keys for the brain and evidence, to avoid shared 429s? Which model does C use for verdicts?
3. **A.** Is `final` one Deepgram `is_final` segment (fine: B reassembles)? Is a Pause ≥800 ms of silence on server time?
4. **C.** Can Verdicts carry `short_fact` / `numeric` by 20:00? Does confidence ≥0.75 mean "I'd bet on it"?
5. **D.** Which TTS provider (first-byte latency)? Will D build the `?audio=1` tab that sends `TTSState`, and pre-cache the fixed templates?
6. **Process.** Who is the backup integrator overnight? Which laptop is the demo machine (and does it drive the projector audio)?
7. **PMQs segment.** In or out? Recommendation: out for the live demo, or recorded only. If in, it needs >2 speakers and Gov/Opp sides, which the brain already supports.
