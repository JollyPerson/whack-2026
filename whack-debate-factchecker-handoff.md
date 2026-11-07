# Handoff: WHACK hackathon — Live Debate Fact-Checker (incident.io track)

**Written:** Sat 7 Nov 2026, ~16:00 Europe/London
**Next session's job:** In Claude Code, produce the team documents — one shared overview so everyone is on the same page, plus one task brief per teammate — from the design below. No implementation yet unless asked.

---

## 1. Context

- Event: **WHACK** student hackathon, London. **Judging is midday Sunday 8 Nov 2026.**
- Chosen track: **incident.io**. Brief (verbatim gist): "Most AI is built to answer after the fact… the most interesting moments are over before you could type a prompt. Build an agent that reacts to a live feed as it happens, with personality. Point it at a camera, microphone, screen or a game and make it respond in the moment." Prize: Asus ZenScreen portable monitor per team member.
- Note: the brief is **not incident-specific** — judging axes implied are (1) proactive/unprompted reaction, (2) latency, (3) personality, (4) live feed.
- Team size: **unknown — assume 4** (plan includes how to adapt to 3 or 5). Teammate names not captured; ask the user.

## 2. How we got here (decisions already made — don't relitigate)

- Rejected: plain sports/game commentator. An F1 replay commentator already exists (Hugging Face Space `d10g/f1commentator`, template-based, historical replays) — too derivative.
- Rejected: "narrator" ideas (planes, Wikipedia edits, market race, nature doc). User's critique, which we adopted: *narration is glorified spoken accessibility, not an agent.* **Test for the idea: the agent must decide something AND its actions must change what happens next.**
- Chosen: **live debate fact-checker + moderator agent**. It fact-checks claims as they're spoken, builds an argument map, and *interjects* (by voice) at pauses — which changes how the debate proceeds. That satisfies the agent test.
- Source choice for the "real material" segment: **UK Parliament (PMQs) beats Oxford Union** — claim-dense, balanced, verifiable against ONS etc., Hansard for transcript ground truth, and Full Fact's PMQs checks as an accuracy benchmark. Commons doesn't sit at weekends → use a recording streamed as-if-live.
- Guardrails agreed: speaker-agnostic and politically balanced; dry referee persona (not snarky — kills credibility, and parliamentary footage licence terms may restrict satire/comedy use — **verify**); default verdict is "unverifiable" when unsure; every verdict shows its source.
- Prior art to acknowledge in pitch: Full Fact (UK, has AI tools), Duke Reporters' Lab live fact-checking, ClaimBuster. Our angle: speed, argument map, agent that moderates/interjects.
- Process principle: **build specific first, generalise later** — don't build a framework before it works on one real feed.

## 3. The pipeline (agreed design)

```
Mic(s) (in-person or online)
  → per-speaker channels → streaming STT (interim + final)
  → claim & argument extraction (+ argument map)
  → retrieval: pre-researched vector index (fast) → web search fallback → fact-check cache
  → verdict (supported / contradicted / missing context / unverifiable) + evidence quote
  → Director decides whether/when to interject
  → outputs: live dashboard (main) + sparing TTS interjections at pauses
```

Key techniques:
- **Speaker separation:** avoid mixed-mic diarization. One channel per speaker — e.g. each speaker's phone opens a web page streaming its mic. Online: per-participant tracks (e.g. LiveKit). Fallback: Deepgram streaming diarization.
- **Only run downstream on *final* STT segments**; show interim text in UI. Keyterm-prime STT for numbers/topic vocab.
- **Pre-research is the speed trick:** given the motion, agent spends ~60–90 s searching (≈20 queries), fetching, chunking, embedding into a vector index before the debate. In-debate retrieval ≈50 ms. Web search (Tavily/Exa/Brave) is the 1–3 s fallback. Cache via Google Fact Check Tools API (ClaimReview).
- **Anti-hallucination guard:** verdict must quote evidence; code checks the quote string actually exists in the retrieved source, else verdict dropped. Numeric claims compared deterministically in code.
- **Director policy:** interject only after a `Pause` (VAD end-of-turn), only high confidence/importance, rate-limited (~1 per 30 s), also prompts sides on unanswered points.
- **Latency budget:** final STT ~1 s → claim highlighted +0.3–0.5 s ("checking…" chip) → index retrieval +0.05 s → verdict +1–2 s ⇒ **~2–4 s end-to-end** (5–7 s via web). Parallelise checks, drop stale ones.

## 4. Shared contracts (freeze in `schemas.py`, owned by integration lead)

```
Utterance    {id, speaker, text, t_start, t_end, final}
Pause        {speaker, t}
Claim        {id, utterance_id, speaker, text, type, checkable, links[{claim_id, supports|attacks}]}
Verdict      {claim_id, label, confidence, evidence[{url, title, quote}], explanation, latency_ms}
SourceCard   {claim_id, url, title, snippet}
Interjection {id, text, priority, claim_id}
```
Transport: single Python process (FastAPI + asyncio queues as bus), websocket fan-out to the dashboard, **every event logged to JSONL** (replay for debugging + demo backup). Everything carries wall-clock timestamps → latency meter for free.

## 5. Team split (4 people)

| Role | Owns | Skills | In → Out | Done when |
|---|---|---|---|---|
| **A — Ears** | Audio capture, per-speaker channels (phone web page), streaming STT, VAD pauses; mock replayer of recorded transcript | audio, websockets, streaming APIs | mics → `Utterance`, `Pause` | 2 live speakers, correct labels, final text <1.5 s |
| **B — Brain + integration lead** | Claim extraction LLM, argument map state (unanswered points, non-responsive rebuttals), Director/interjection policy, `schemas.py`, bus | prompting, LLM APIs, state machines | `Utterance`,`Pause`,`Verdict` → `Claim`,`Interjection` | <500 ms/sentence, sensible map, interjections well-timed not spammy |
| **C — Evidence (strongest person)** | Pre-research job, vector index, web fallback, fact-check cache, verdicts with quote verification + numeric checks, **20-claim labelled eval set** | search APIs, embeddings, careful LLM eval | motion,`Claim` → `Verdict`,`SourceCard` | <2 s from index, zero fabricated quotes, good eval score |
| **D — Face** | Dashboard (speaker-coloured transcript, claim chips, source cards, per-side argument map, scoreboard, latency meter, motion picker + pre-research progress), TTS moderator voice, demo script, backup video | frontend, TTS, presenting | all events → UI + audio | replayed test debate looks great on projector with sound |

Adapting: 3 people → merge A + D. 5 people → split C into (pre-research + retrieval) and (verdicts + eval set).

Kickoff (first 30 min, everyone): freeze schemas; push repo skeleton; **record a 3-minute test debate between two teammates** + timestamped transcript = shared fixture all mocks replay.

## 6. Timeline

| Time | Milestone |
|---|---|
| ~16:15–16:30 Sat | Schemas frozen, skeleton pushed, test debate recorded |
| 18:30 | Every module works standalone on mocks |
| 20:00 | **First end-to-end on recorded debate** (ugly OK) |
| 21:30 | Live mics end-to-end |
| Overnight | Verdict accuracy, interjection timing, UI polish |
| 09:00 Sun | Feature freeze; record backup video |
| 09:00–11:30 | Rehearse demo ≥3× with a fresh motion |
| ~12:00 | Judging |

## 7. Demo plan

1. Judge picks a motion → agent visibly pre-researches (~60 s, during intro).
2. Two judges debate ~2 min (light motion is fine). Claims light up, verdicts land, moderator interjects once or twice.
3. End on argument map + scoreboard ("who actually won on the evidence").
4. Optional: ~1 min PMQs clip streamed as-if-live, with accuracy vs Full Fact.
Pitch hook using their framing: "By the time you'd typed a prompt about that claim, our agent had already checked it."

## 8. Open questions for the user (ask early in next session)

- Team size and names; who takes which role (C should be the strongest/most careful).
- Choice of providers: LLM (fast model for claims/verdicts), STT (Deepgram vs AssemblyAI), TTS (ElevenLabs/Cartesia/OpenAI), search API, embeddings.
- Where docs should live: repo `docs/` folder (markdown) vs shared Notion/Google doc. User has Notion connected.
- Does the team already have a repo? (User's GitHub: see their profile; don't hard-code.)
- Verify parliamentary recording licence terms before using PMQs footage in the demo.

## 9. What the next session should produce

1. `docs/OVERVIEW.md` — the idea, why it fits the brief, the agent test, pipeline diagram, contracts, timeline, demo plan, guardrails. One page everyone reads first.
2. `docs/briefs/A-ears.md`, `B-brain.md`, `C-evidence.md`, `D-face.md` — each self-contained: goal, inputs/outputs (link to schemas), tasks in order, mocks to build/use, definition of done, stretch goals, who they depend on and when.
3. `schemas.py` draft (or `docs/CONTRACTS.md`) matching §4.
4. Optionally `docs/GLOSSARY.md` (Utterance, Claim, Verdict, Director, Pre-research, Interjection…).
5. Optionally tracer-bullet tickets per role with blocking edges.

Keep docs short and scannable — teammates are mid-hackathon.

## 10. Suggested skills

- `mattpocock-skills:to-spec` — synthesise this handoff into the overview spec (no interview needed).
- `mattpocock-skills:to-tickets` — break the plan into per-role tracer-bullet tickets with blocking edges.
- `mattpocock-skills:domain-modeling` — for `GLOSSARY.md` and pinning terminology across briefs.
- `mattpocock-skills:writing-for-agents` — if any briefs will be handed to coding agents (CLAUDE.md / AGENTS.md style).
- `mattpocock-skills:setup-matt-pocock-skills` — only if the repo needs its issue tracker/doc layout configured before `to-spec`/`to-tickets`.
- `anthropic-skills:docs` — only if the user wants shareable Claude Docs instead of repo markdown.

## Notes for the agent

- User preference: **do not add Claude as co-author / attribution lines on commits** to their repos.
- Redacted: user's personal details and email intentionally omitted.
