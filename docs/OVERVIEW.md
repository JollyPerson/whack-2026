# Live Debate Fact-Checker: Project Overview

An agent that listens to a live debate, checks claims as they are spoken, and steps in by voice at pauses. What it says changes how the debate goes next.

Built for **WHACK** (student hackathon, London), incident.io track. **Judging is about midday on Sunday 8 November 2026.**

**Status:** planning. No product code exists yet. Items marked **Open** aren't decided, so check the [planning map](https://github.com/JollyPerson/whack-2026/issues/1) for the latest.

## If you are an AI agent

- Treat this file as the project's source of context. Use the terms in [Vocabulary](#vocabulary) exactly.
- Don't reopen anything under [Settled decisions](#settled-decisions) unless your human asks you to.
- Don't assume an **Open** item is decided. Check the map, then ask your human.
- Don't change the [contracts](#shared-contracts) by yourself. Other people's modules depend on them, so raise changes with the integration lead.
- Build for one real feed first. Don't build a general framework.
- Don't add Claude attribution or co-author lines to commits.

## The brief

The track asks for an agent that reacts to a live feed as it happens, with personality, rather than answering after the fact. It suggests pointing the agent at a camera, microphone, screen or game. The prize is an Asus ZenScreen portable monitor per team member.

The brief isn't specific to incidents. We read the implied judging criteria as:

1. **Proactive:** it reacts without being prompted.
2. **Latency:** it responds in the moment.
3. **Personality:** it has a recognisable character.
4. **Live feed:** it works on real input as it happens.

**Pitch hook:** "By the time you'd typed a prompt about that claim, our agent had already checked it."

## Settled decisions

**The agent test.** The agent must decide something, and its actions must change what happens next. Narration fails this test because it only describes what is happening.

- **Rejected:** sports or game commentary, because it's derivative (an F1 replay commentator already exists).
- **Rejected:** narrator ideas such as planes, Wikipedia edits, market races and nature documentaries, because they fail the agent test.
- **Chosen:** a live debate fact-checker and moderator. It checks claims, builds an argument map, and interjects by voice, which changes how the debate proceeds.

**Guardrails**

- It is speaker-agnostic and politically balanced.
- The persona is a dry referee, never snarky. Snark costs credibility.
- When unsure, the verdict is `unverifiable`.
- Every verdict shows its source.

**Prior art to acknowledge in the pitch:** Full Fact, Duke Reporters' Lab live fact-checking, and ClaimBuster. Our angle is speed, the argument map, and an agent that moderates and interjects.

## How it works

```
Mic(s), in person or online
  → one channel per speaker → streaming speech-to-text (interim + final)
  → claim and argument extraction (+ argument map)
  → retrieval: pre-researched vector index (fast) → web search fallback → fact-check cache
  → Verdict (supported / contradicted / missing context / unverifiable) + evidence quote
  → Director decides whether and when to speak
  → outputs: live dashboard (main) + occasional spoken Interjections at pauses
```

**Key techniques**

- **One channel per speaker:** avoid separating voices from a single mixed mic. Each speaker's phone opens a web page that streams its mic. Online, use per-participant tracks (for example LiveKit). Streaming diarization is the fallback.
- **Final text only:** only final speech-to-text segments go downstream. Interim text is only shown in the UI. Prime the speech-to-text with numbers and topic vocabulary.
- **Pre-research is the speed trick:** given the motion, the agent spends about 60–90 s searching (about 20 queries), fetching, chunking and embedding into a vector index. Lookups during the debate then take about 50 ms. Web search is the 1–3 s fallback.
- **Anti-hallucination guard:** a verdict must quote its evidence. Code checks that the quote really exists in the retrieved source, and drops the verdict if it doesn't. Numeric claims are compared in code, not by the LLM.
- **Director policy:** speak only after a Pause, only for high-confidence and important points, and about once per 30 s at most. It can also prompt a side about a point it hasn't answered.
- **Stale checks:** run checks in parallel and drop any that are out of date.

**Latency budget**

| Step | Target |
|---|---|
| Final speech-to-text | about 1 s |
| Claim highlighted with a "checking..." chip | +0.3–0.5 s |
| Index retrieval | +0.05 s |
| Verdict | +1–2 s |
| **End to end** | **about 2–4 s** (5–7 s via web search) |

## Shared contracts

This is a **draft and isn't frozen**. Freezing it is tracked in [Are the shared contracts complete enough to freeze?](https://github.com/JollyPerson/whack-2026/issues/3). The integration lead will own the final version in `schemas.py`.

```
Utterance    {id, speaker, text, t_start, t_end, final}
Pause        {speaker, t}
Claim        {id, utterance_id, speaker, text, type, checkable, links[{claim_id, supports|attacks}]}
Verdict      {claim_id, label, confidence, evidence[{url, title, quote}], explanation, latency_ms}
SourceCard   {claim_id, url, title, snippet}
Interjection {id, text, priority, claim_id}
```

**Runtime design**

- A single Python process: FastAPI, with asyncio queues as the event bus.
- A websocket fans events out to the dashboard.
- Every event is logged to JSONL, for debugging replays and as a demo backup.
- Every event carries wall-clock timestamps, which gives us the latency meter for free.

## Team and roles

We are 3 people (GitHub: `JollyPerson`, `dabestthebest`, `dy-dt`). Ears, Brain and Evidence are separate roles with one person each. Face has no owner yet, and someone picks it up later.

**Open:** who takes which role, and who picks up Face and when. See [Who owns each role and the unowned Face work?](https://github.com/JollyPerson/whack-2026/issues/2). Evidence should go to the strongest, most careful person.

### Ears

- **Owns:** audio capture, per-speaker channels (the phone web page), streaming speech-to-text, pause detection, and a mock replayer for a recorded transcript.
- **Skills:** audio, websockets, streaming APIs.
- **In → out:** mics → `Utterance`, `Pause`.
- **Done when:** two live speakers are labelled correctly and final text arrives in under 1.5 s.

### Brain (and integration lead)

- **Owns:** claim extraction, argument map state (unanswered points, non-responsive rebuttals), the Director, `schemas.py`, and the event bus.
- **Skills:** prompting, LLM APIs, state machines.
- **In → out:** `Utterance`, `Pause`, `Verdict` → `Claim`, `Interjection`.
- **Done when:** extraction takes under 500 ms per sentence, the map makes sense, and Interjections are well timed rather than spammy.

### Evidence

- **Owns:** the pre-research job, vector index, web fallback, fact-check cache, verdicts with quote verification and numeric checks, and a **20-claim labelled eval set**.
- **Skills:** search APIs, embeddings, careful LLM evaluation.
- **In → out:** motion, `Claim` → `Verdict`, `SourceCard`.
- **Done when:** verdicts take under 2 s from the index, there are no fabricated quotes, and the eval score is good.

### Face (no owner yet)

- **Owns:** the dashboard, the Moderator voice, the demo script and the backup video.
- **Dashboard contents:** speaker-coloured transcript, claim chips, source cards, per-side argument map, scoreboard, latency meter, motion picker and pre-research progress.
- **Skills:** frontend, text-to-speech, presenting.
- **In → out:** all events → UI and audio.
- **Done when:** a replayed test debate looks great on a projector with sound.

## Timeline

| Time | Milestone |
|---|---|
| By 17:00 Sat | Planning stops. Schemas frozen, repo skeleton pushed, test debate recorded |
| 18:30 | Every module works standalone on mocks |
| 20:00 | **First end-to-end run on the recorded debate** (ugly is fine) |
| 21:30 | Live mics end to end |
| Overnight | Verdict accuracy, Interjection timing, UI polish |
| 09:00 Sun | Feature freeze, record the backup video |
| 09:00–11:30 | Rehearse the demo at least 3 times with a fresh motion |
| about 12:00 | Judging |

**Kickoff, everyone together:** freeze the schemas, push the repo skeleton, and record a 3-minute test debate between two teammates with a timestamped transcript. That recording is the shared fixture all mocks replay, so nobody has to wait on anyone else's module.

## Demo plan

1. A judge picks a motion, and the agent visibly pre-researches for about 60 s during the intro.
2. Two judges debate for about 2 minutes. A light motion is fine. Claims light up, verdicts land, and the Moderator interjects once or twice.
3. End on the argument map and scoreboard: who actually won on the evidence.

**Open:** whether to add a clip of about 1 minute from PMQs (Prime Minister's Questions), streamed as if live, with accuracy compared against Full Fact. The Commons doesn't sit at weekends, so it would have to be a recording. Research found the footage terms look like a problem: don't stream footage without written permission from Parliament's Recording Unit. Hansard text looks usable with attribution, as a replayed `Utterance` stream or read aloud by teammates. This isn't legal advice, and the live terms still need checking in a browser. See [Do we commit to the PMQs segment?](https://github.com/JollyPerson/whack-2026/issues/10).

## If we fall behind

Cut in this order:

1. Fact-check cache
2. Prompts to a side about unanswered points
3. Scoreboard
4. Argument map, reduced to a plain per-side list

**Never cut:** the per-speaker phone page, verdicts with verified quotes, the latency meter and spoken Interjections.

## Providers

**Open.** The team has credits, so we are comparing on latency and quality rather than cost. Research results and the final choice are tracked in [Which providers do we use?](https://github.com/JollyPerson/whack-2026/issues/9). This covers the LLM, speech-to-text, text-to-speech, search and embeddings. Findings so far are recommendations, not commitments, and nothing has been latency-tested yet.

## Vocabulary

- **Motion:** the statement being debated.
- **Utterance:** a segment of speech-to-text from one speaker.
- **Pause:** a detected end of a speaker's turn. It is the only moment the Moderator may speak.
- **Claim:** a statement extracted from an Utterance, which may or may not be checkable.
- **Verdict:** the result of checking a Claim: supported, contradicted, missing context or unverifiable, with its evidence.
- **Pre-research:** the job that builds the evidence index for a Motion before the debate starts.
- **Director:** the policy that decides whether and when the agent speaks.
- **Moderator:** the agent's on-air persona: the voice and dry referee tone. Avoid "narrator" or "commentator".
- **Interjection:** a single thing the Director has decided the Moderator should say.

## Where things live

- **Planning map:** [Map: Live Debate Fact-Checker team plan](https://github.com/JollyPerson/whack-2026/issues/1). Decisions and open questions are listed here, and the original handoff is its first comment.
- **Issue tracker conventions:** `docs/agents/issue-tracker.md`
- **Domain docs rules:** `docs/agents/domain.md`
- **Glossary:** `GLOSSARY.md`
