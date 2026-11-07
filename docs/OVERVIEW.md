# Live Debate Fact-Checker: Project Overview

An agent that listens to a live debate, checks claims as they are spoken, and steps in by voice at pauses. What it says changes how the debate goes next.

Built for **WHACK** (student hackathon, London), incident.io track. **Judging is about midday on Sunday 8 November 2026.**

**Status:** building. Brain, the bus and the mocks are in the repo; nothing has run end to end yet. Items marked **Open** aren't decided, so check the [planning map](https://github.com/JollyPerson/whack-2026/issues/1) for the latest.

## If you are an AI agent

- Treat this file as the project's source of context. Use the terms in [Vocabulary](#vocabulary) exactly.
- Don't reopen anything under [Settled decisions](#settled-decisions) unless your human asks you to.
- Don't assume an **Open** item is decided. Check the map, then ask your human.
- Treat techniques, numbers and provider notes as starting ideas. Each role owner researches and decides for their role.
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

**Starting ideas**

These came from early planning and haven't been tested. Role owners can replace any of them, and better ideas are welcome.

- **One channel per speaker:** avoid separating voices from a single mixed mic, for example by having each speaker's phone stream its mic from a web page.
- **Final text only:** only final speech-to-text segments go downstream. Interim text is only shown in the UI.
- **Pre-research:** build an evidence index for the motion before the debate, so lookups during it are fast. Web search is the slower fallback.
- **Anti-hallucination guard:** a verdict must quote its evidence. Code checks that the quote really exists in the retrieved source, and drops the verdict if it doesn't. Numeric claims are compared in code, not by the LLM.
- **Director policy:** speak only after a Pause, only for high-confidence and important points, and not too often. It can also prompt a side about a point it hasn't answered.
- **Stale checks:** run checks in parallel and drop any that are out of date.

**Latency budget**

These are targets, and nothing has been measured yet.

| Step | Target |
|---|---|
| Final speech-to-text | about 1 s |
| Claim highlighted with a "checking..." chip | +0.3–0.5 s |
| Index retrieval | +0.05 s |
| Verdict | +1–2 s |
| **End to end** | **about 2–4 s** (5–7 s via web search) |

## Shared contracts

**Frozen for now at v1.0.** The source of truth is `app/schemas.py`, explained in `docs/CONTRACTS.md`. Existing field names don't change. New fields are added as optional, and extra fields and new event types pass through the bus, so tell the integration lead if one should become official.

**Runtime design**

- A single Python process: FastAPI, with asyncio queues as the event bus.
- A websocket fans events out to the dashboard.
- Every event is logged to JSONL, for debugging replays and as a demo backup.
- Every event carries wall-clock timestamps, which gives us the latency meter for free.

## Team and roles

We are 3 people (GitHub: `JollyPerson`, `dabestthebest`, `dy-dt`).

| Role | Owner | Ticket |
|---|---|---|
| Ears | Thomas (`dy-dt`) | [Ears](https://github.com/JollyPerson/whack-2026/issues/11) |
| Brain and integration lead | Tong (`dabestthebest`) | [Brain](https://github.com/JollyPerson/whack-2026/issues/12) |
| Evidence, eval set and demo script | Harkaran (`JollyPerson`) | [Evidence](https://github.com/JollyPerson/whack-2026/issues/15) |
| Face (after Ears) | Thomas (`dy-dt`) | [Face](https://github.com/JollyPerson/whack-2026/issues/16) |

Each owner researches their area, chooses their approach and providers, and shares what they chose and why on their ticket.

**Falling behind:** hold a 5-minute check-in right after the 20:00 run. Tong makes the call as integration lead, starting with the cut list. If that isn't enough, the person furthest ahead helps whoever is blocking the pipeline.

### Ears

- **Owner:** Thomas.
- **Owns:** audio capture, per-speaker channels (the phone web page), streaming speech-to-text, pause detection, and a mock replayer for a recorded transcript.
- **Skills:** audio, websockets, streaming APIs.
- **In → out:** mics → `Utterance`, `Pause`.
- **Done when:** two live speakers are labelled correctly and final text arrives in under 1.5 s.

### Brain (and integration lead)

- **Owner:** Tong.
- **Owns:** claim extraction, argument map state (unanswered points, non-responsive rebuttals), the Director, `schemas.py`, and the event bus.
- **Skills:** prompting, LLM APIs, state machines.
- **In → out:** `Utterance`, `Pause`, `Verdict` → `Claim`, `Interjection`.
- **Done when:** extraction takes under 500 ms per sentence, the map makes sense, and Interjections are well timed rather than spammy.
- **Extraction support:** Harkaran labels the gold claims and, after the 20:00 run, tests a faster extraction model through Brain's provider switch. Tong still owns Brain.

### Evidence

- **Owner:** Harkaran.
- **Owns:** the pre-research job, vector index, web fallback, fact-check cache, verdicts with quote verification and numeric checks, a **20-claim labelled eval set**, and the demo script. The fact-check cache is stretch work, started only if the eval gates pass and the 21:30 live-mic test shows slow web-search checks are hurting.
- **Skills:** search APIs, embeddings, careful LLM evaluation.
- **In → out:** motion, `Claim` → `Verdict`, `SourceCard`.
- **Done when:** verdicts take under 2 s from the index, there are no fabricated quotes, and the eval gates pass. The eval design and gates are on [the Evidence ticket](https://github.com/JollyPerson/whack-2026/issues/15).

### Face

- **Owner:** Thomas, starting once Ears works standalone at 18:30. Ears comes first.
- **Owns:** the dashboard, the Moderator voice and the backup video.
- **Build order:** transcript, claim chips and verdicts with sources; latency meter; Moderator voice (working by 21:30); Motion picker and pre-research progress; argument map; scoreboard.
- **Final screen:** a plain per-side list of claims and verdicts is the guaranteed version. The argument map and scoreboard replace it if they're ready.
- **Dashboard contents:** speaker-coloured transcript, claim chips, source cards, per-side argument map, scoreboard, latency meter, motion picker and pre-research progress.
- **Skills:** frontend, text-to-speech, presenting.
- **In → out:** all events → UI and audio.
- **Done when:** a replayed test debate looks great on a projector with sound.

## Timeline

| Time | Milestone |
|---|---|
| By 17:00 Sat | Planning stops. Schemas frozen, repo skeleton pushed, test debate recorded |
| 18:30 | Every module works standalone on mocks |
| 19:00 | Recorded test debate and transcript in `fixtures/` (Thomas and Tong), with 8–10 checkable facts covering each verdict label. 5 demo Motions chosen (Harkaran) |
| 19:00–19:30 | Gold claim labelling (Harkaran) |
| 20:00 | **First end-to-end run on the recorded debate** (ugly is fine), watched through the JSONL log. If there's no recording, it uses the fake debate. Check-in follows |
| 20:30–21:15 | Write and freeze the verdict eval set (Harkaran) |
| 21:30 | Live mics end to end, with the Moderator voice working |
| Overnight | Verdict accuracy, Interjection timing, UI polish. Tuning stops at 06:00. Harkaran covers integration while Tong sleeps (01:45–05:45) |
| 09:00 Sun | Feature freeze, presenter picked, Thomas records the backup video |
| 09:00–11:30 | Rehearse the demo at least 3 times, using Motions from the list |
| 11:00 | Decide whether judges debate or teammates do |
| about 12:00 | Judging |

**Kickoff, everyone together:** freeze the schemas, push the repo skeleton, and record a 3-minute test debate between two teammates with a timestamped transcript. That recording is the shared fixture all mocks replay, so nobody has to wait on anyone else's module.

## Demo plan

The slot is **3 minutes**. This plan assumes setup and Q&A aren't included, which still needs checking with the organisers. Everything is connected before the slot starts.

| Time | What happens |
|---|---|
| 0:00–0:20 | The hook, and a judge picks a Motion from the list |
| 0:20–2:15 | The debate, with claims, verdicts and at least one spoken Interjection |
| 2:15–3:00 | The final screen, the agent's effect on the debate, and a nod to prior art |

- **Motions:** the judge chooses from 5 tested Motions that are fun but rich in facts, listed in `app/evidence/motions.json`.
- **Evidence indexes:** these are built in advance for every listed Motion, and we say so on stage. Pre-research progress is shown for a few seconds at most, and isn't presented as happening live.
- **Debaters:** two teammates debate while the third presents. Judges debate instead only if a stand-in from outside the team passes a rehearsal test by 11:00 on Sunday: the debate starts within 15 seconds and yields at least two verdicts.
- **Fact cards:** debaters prepare facts for each side of each Motion, including some deliberately wrong ones. Judges are told this upfront.
- **Dry spells:** the Moderator uses a canned line at the first Pause after 25 seconds of debate, such as "Neither side has offered a figure yet. Care to name one?" After that, it waits for the Director's 30-second gap. It skips only the confidence threshold.
- **Demo machine:** Tong's laptop, with the presenter operating it from `?admin=1`.
- **Fallbacks:** live mics are the default. Switch to the recorded debate replayed through the real pipeline if there's no transcript within 15 seconds, or no verdict by 40 seconds into the debate. The presenter says it's a replay. The backup video is only for when the pipeline is down.
- **Open:** team phones on a team hotspot, with venue wifi as the backup, are assumed but not confirmed.

**PMQs is dropped**, along with the Full Fact comparison, which leaves the eval set as the accuracy story. See [Do we commit to the PMQs segment?](https://github.com/JollyPerson/whack-2026/issues/10).

## If we fall behind

Cut in this order:

1. Fact-check cache
2. Prompts to a side about unanswered points
3. Scoreboard
4. Argument map, reduced to a plain per-side list

The Moderator's canned dry-spell line is exempt from item 2.

**Never cut:** the per-speaker phone page, verdicts with verified quotes, the latency meter and spoken Interjections.

## Providers

There's no central pick. Each role owner chooses their own providers, comparing on latency and quality rather than cost because the team has credits.

Earlier desk research is available as a starting point, but it's untested and not binding, so try forming your own view first: [speech-to-text](https://github.com/JollyPerson/whack-2026/issues/4), [LLM](https://github.com/JollyPerson/whack-2026/issues/7), [search and embeddings](https://github.com/JollyPerson/whack-2026/issues/6), and [text-to-speech](https://github.com/JollyPerson/whack-2026/issues/5).

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

## Repo layout

One folder per role under `app/`, so people rarely edit the same files. Modules talk only through the events in `app/schemas.py`, never by importing each other. `README.md` has the run commands and the full layout.

```
app/schemas.py  shared contracts. Owner: Brain
app/bus.py      event bus, JSONL log and replay. Owner: Brain
app/brain/      claims, argument map, Director. Owner: Tong
app/ears/       audio, speech-to-text, pauses, phone mic page. Owner: Thomas
app/evidence/   pre-research, retrieval, verdicts. Owner: Harkaran
app/face/       dashboard, Moderator voice. Owner: Thomas
app/mocks/      stand-ins for each role
ears/           Thomas's uv package, until it moves into app/ears/
fixtures/       the shared recorded test debate and transcript
eval/           extraction eval, plus the 20-claim labelled set and scoring script
docs/           overview, contracts, integration, research, agent docs
```

Each role lists its dependencies in `requirements/<role>.txt`.

## Where things live

- **Planning map:** [Map: Live Debate Fact-Checker team plan](https://github.com/JollyPerson/whack-2026/issues/1). Decisions and open questions are listed here, and the original handoff is its first comment. The handoff is more prescriptive than this overview, and this overview wins where they differ.
- **Issue tracker conventions:** `docs/agents/issue-tracker.md`
- **Domain docs rules:** `docs/agents/domain.md`
- **Glossary:** `GLOSSARY.md`
