# Plugging your part in

You choose your own stack and design. The only shared surface is the event bus. Join it in **any** of
three ways. You can switch later without anyone else changing anything.

## Option 1: in-process Python (lowest latency)

```python
# app/<role>/run.py  -> picked up when the profile says <role>=real
from app.schemas import Claim, Verdict, Evidence

async def run(bus):
    async for ev in bus.subscribe("evidence", {"Claim", "SessionStart"}):
        if ev.event == "SessionStart":
            continue                          # pre-research ev.motion here
        if ev.checkable:                      # never block: use async SDKs / asyncio.to_thread / create_task
            bus.publish(Verdict(claim_id=ev.id, label="supported", confidence=0.9,
                                evidence=[Evidence(url="https://www.ons.gov.uk/...", title="ONS", quote="...")]),
                        src="evidence")
```

- `run` may be `async def` or a plain `def`. It may also **return**, e.g. after starting an SDK thread or
  your own tasks. A module that returns stays green and is never started twice.
- If you need HTTP routes (e.g. A's phone mic page), put a FastAPI `router` in `app/<role>/routes.py`. It is
  mounted automatically. A role with only `routes.py` (or a face with only `static/`) and no `run.py` shows green.
- If your module raises, only your module restarts, with backoff (1, 2, 4, 8, 10 s). It shows as a red dot
  with the error.
- If a package is missing, the red dot says `missing dependency 'x'`. Add the package to your
  `requirements/<role>.txt` and rerun `./run.sh`, which reinstalls when any requirements file changes.
- Is an SDK calling you back on another thread? Use `bus.publish_threadsafe(ev)`.

## Option 2: separate process, any language, over the websocket

Run the server with your role set to `external`, e.g. `EVIDENCE=external ./run.sh drive`. Then connect to `ws://<host>:8000/ws`:

```js
const ws = new WebSocket("ws://localhost:8000/ws");
ws.onopen = () => ws.send(JSON.stringify({op: "subscribe", events: ["Claim", "SessionStart"]}));  // optional filter
ws.onmessage = m => {
  const e = JSON.parse(m.data);               // the first message is a Snapshot of the session so far
  if (e.event === "Claim" && e.checkable)     // a bare event == publish (same as {op: "publish", event: {...}})
    ws.send(JSON.stringify({event: "Verdict", claim_id: e.id, label: "supported", confidence: 0.9, src: "evidence",
                            evidence: [{url: "https://www.ons.gov.uk/...", title: "ONS", quote: "..."}]}));
};
```

An array of events works too.

Other ops: `{"op":"session_start","motion":"...","speakers":{"A":{"name":"Ali","side":"prop"}}}` and
`{"op":"control","flag":"mute","value":true}`. Extra fields on both pass through.

Errors come back as `{"event":"Error","detail":...,"op":...}` with no `seq`. That covers an invalid event, a
non-JSON frame and an unknown op. The socket keeps working after an error, and a rejected array publishes
nothing.

## Option 3: HTTP

```bash
curl -X POST localhost:8000/publish -H 'content-type: application/json' \
     -d '{"event":"Utterance","speaker":"A","text":"Unemployment is 6 percent.","final":true}'
curl -X POST localhost:8000/session -d '{"motion":"This house would ban cars","speakers":{"A":{"side":"prop"},"B":{"side":"opp"}}}' -H 'content-type: application/json'
curl localhost:8000/health        # modules, flags, current log file
```

`/publish` also accepts a JSON array. It returns the events with `seq`/`ts` filled in. If any event is
invalid, it replies 422 with the reason and publishes none of them, so a retry never duplicates anything.
`/session` and `/control` also reply 422 to a bad body.

---

## What the brain (B) needs from each of you, and what it tolerates

**A: Ears**
- Needs: final `Utterance` events with a stable `speaker` per mic.
- **Strongly recommended: interim Utterances, or `Pause` at end of turn (~800 ms of silence), ideally both.**
  These are how the referee knows someone is still talking.
  - With interims (same `id` as the final, `final: false`), the brain can tell talking from silence. The floor
    opens at your Pause, or 2.5 s after the last speech if a Pause goes missing. If you send no Pauses at all,
    it synthesises one about 1.2 s after the last speech.
  - With finals only, a final is no proof the speaker stopped, because the next final may be a whole sentence
    away. So the brain is conservative. Without your Pause it waits at least 6 s of silence after the last final
    (longer after a long final, up to 15 s) before it speaks or synthesises a Pause. Interjections then arrive
    late or not at all.
  - A real `Pause` opens the floor at once in both cases.
- Tolerates:
  - Sentence fragments: B reassembles split finals.
  - Missing or unusable timestamps: B falls back to `ts`. Stream-relative seconds (e.g. Deepgram
    `start`/`end`) sent over `/ws` or `/publish` are treated as unknown.
  - No `Pause` at all: B synthesises Pauses (see the timings above) until real ones arrive.
  - Deepgram-style payloads: `speaker: 0` (an integer), `is_final`, and `null` fields.
  - Mic bleed: B drops the other speaker's voice leaking into a mic.
  - TTS echo: B drops the moderator's own voice.
- Nice to have:
  - `t_start`/`t_end` on the server clock;
  - gating your mic frames while `TTSState.speaking` is true.

**C: Evidence**
- Needs: a `Verdict` per checkable `Claim`. Every non-`unverifiable` label needs at least one `evidence` item.
- Tolerates:
  - Fact-checker labels: "Mostly false", "Pants on Fire", "Half true", "Accurate", FEVER's
    SUPPORTS/REFUTES/NOT ENOUGH INFO and more (see CONTRACTS). Unknown labels become `unverifiable`, with a
    warning in the server log.
  - Confidence as a percentage (`85`, `"85%"`).
  - A float `latency_ms`, `null` fields, and evidence given as bare URL strings.
  - Re-sending a better Verdict later. It replaces the chip. The referee speaks at most one verdict line per
    claim, though.
  - Extra fields.
- Nice to have:
  - `short_fact` (≤10 words) and `numeric{claimed, actual, unit}`, so the referee can say *"the ONS puts that at 4.1 percent, not 6"* instead of a generic line;
  - `confidence ≥ 0.75` meaning "I'd bet on it";
  - `app.latency.finish(verdict, claim)` for the latency meter.
- Pre-research hook: subscribe to `SessionStart` (it carries `motion`) and publish `ResearchProgress`.
- Need an LLM for verdicts? Reuse B's client: `from app.brain.llm import make_llm; llm = make_llm()`, then
  `await llm.complete_json(system, user, your_json_schema, timeout=2.0)`.
  - It works with Claude or ChatGPT, whichever key is in `.env`. `app/config.py` loads `.env` on import, so
    your own process gets the key too. It also works with any OpenAI-compatible server via `LLM_BASE_URL`.
  - `make_llm()` returns `None` when no key is set, so check for that.
  - `complete_json` never takes longer than `timeout`. On failure it raises `LLMError` (subclasses
    `LLMTimeout`, `LLMRefusal`, `LLMInvalid`), so catch it and fall back.
  - Your schema is normalised to strict form automatically, so it behaves the same on both providers:
    - optional fields become nullable, so expect `null` for a property you didn't list in `required`;
    - unsupported keywords like `minimum`/`maximum` are dropped from the schema (they're kept only as a hint
      in the description), so check ranges in your own code.

**D: Face**
- Needs to render: `Utterance` (replace interims by `id`), `Claim` (chip), `Verdict` (chip colour + source), `SourceCard`, `ArgMapSnapshot` (full state, no logic needed), `Interjection` (speak `text`, show `evidence[0]`), `DirectorDecision`/`DirectorStatus` (the "why it spoke / why it's silent" panel), `ModuleStatus`.
- The reference client at `/debug` (`app/tools/ws_debug.html`) implements all of it in plain JS, so you can copy the reducer.
- Must:
  - Only **one** tab plays audio: `/?audio=1`. It sends `TTSState{speaking:true/false, interjection_id}` around playback.
  - Never speak Interjections that arrive in the initial Snapshot. That would replay all past audio on a refresh.
    The rule is to speak only live events whose `seq` is greater than `Snapshot.last_seq`. During
    `./run.sh replay`, recorded Interjections arrive live, so they are spoken once. After a refresh they are
    history and stay silent.
  - The admin keys send `Control`: M mute, F force next, 1–5 aggressiveness, W web fallback, I interims, Esc mute.
- Serve your page by putting it at `app/face/static/index.html` (served at `/`), or host it anywhere and point it at `/ws`.
- Voice: `GET /face/tts?text=` returns the line as a cached 24 kHz mono WAV (ElevenLabs or espeak-ng; see
  `app/face/README.md`). `TTSState{speaking:true}` means "the referee is audible now".

**Mate Engine bridge (protocol v1).** A desktop avatar (our Mate Engine fork) shows the line in a speech bubble and
moves its mouth while the `?audio=1` tab plays it. It is a pure consumer: it publishes nothing, and it never plays audio.

```
GET /face/mate/next?after=<cursor:int>&epoch=<string>&wait=<seconds:int, default 10, max 25>
-> 200 {"v":1,"epoch":"<8 hex>","cursor":<int>,"cmds":[Cmd,...]}
Cmd = {"op":"say"|"talk"|"stop"|"dance"|"dance_stop","id":"","text":"","audio":"","title":"","on":false,"clear":false,"seconds":0}
```

- Every field is always present and never null (Unity's `JsonUtility` handles neither).
- `say`: `id`/`text` are the Interjection's, exactly as shown in the bubble. `audio` is the WAV's relative URL
  (`/face/tts?text=<urlencoded>`), or `""` when the server has no voice. An Interjection older than 4 s at delivery
  is dropped.
- `talk`: `id` = `TTSState.interjection_id` (or `""`), `on` = `TTSState.speaking`. The mouth follows the one audio player.
- `stop`: `clear=true` (remove the bubble, close the mouth) on `Control` mute=true, `Control` director=false and
  `SessionStart`; `clear=false` (close the mouth, keep the bubble) on `Control` tts=false.
- `dance` (`title`, `""` = the first available) and `dance_stop` come only from the operator routes
  `POST /face/mate/dance {"title":"","channel":""}` and `POST /face/mate/dance/stop {"channel":""}` (`channel`:
  one avatar, or `""`/absent for every one). They are not bus events: each is held for up to 10 s and delivered
  once, to that channel's next poll.
- `dance` also carries `on` (play the dance's music; false = silent) and `seconds` (stop after this long; 0 = play
  to the end). Older builds ignore both and play with their configured volume to the end.
- **Verdict dances.** On the `for` and `against` channels, `DebateResult` becomes `dance` with
  `title=dances[channel]`, `on=music[channel]`, `seconds=dance_s`, and `SessionStart` also sends `dance_stop`.
- **Channels (side bots).** `GET /face/mate/next/{channel}` takes the same query and returns the same payload, one
  avatar per channel. `referee` is exactly the route above (`/face/mate/next` = `/face/mate/next/referee`). `for` and
  `against` carry only that side bot's lines: `BotLine{persona}` becomes `say` (`audio: ""`) plus `talk on` (dropped,
  like an Interjection, if older than 4 s at delivery), and `BotTalk{persona, on:false}` becomes `talk off`.
  `SessionStart`, `Control` mute=true and `Control` bots=false send `stop` with `clear=true`. Any other channel is a
  404. Each channel has its own status dot (`ModuleStatus` `mate`, `mate_for`, `mate_against`), published only once
  that avatar has polled. A Mate Engine instance picks its channel with `pollPath` in its own `speech_bridge.json`
  (`"/face/mate/next/for"`); a `?channel=` query cannot work, because the client appends `?after=` itself.
- Start with `after=-1`. Send back the `cursor` and `epoch` of the last reply. If `after<0`, the epoch differs, or
  `after` is ahead of the server (it restarted), the reply is immediate: `{"cursor": <now>, "cmds": []}`. Past events
  are never replayed. Otherwise the reply comes as soon as there is at least one command, or after `wait` seconds.

---

## Checkpoints (B runs these on a fresh clone and posts pass/fail)

- **18:30, standalone:** your part works against mocks. Use `./run.sh dev-a|dev-c|dev-d`, or your process with your role set to `external`.
- **20:00, first E2E:** `./run.sh drive` runs the full 3-min recorded debate.
  - No red dots, ≥5 verdicts with sources, no stuck "checking…" chips.
  - ≥1 interjection, spoken only at a pause.
  - A browser refresh recovers state.
  - The good run's log is saved: `cp logs/latest.jsonl fixtures/golden_run.jsonl`.
- **21:30, live mics:** a fresh motion and two people talking, with interjections ≤1 per 30 s and never over a speaker.

The fixture format is the log format. To record your own fixture, run anything and copy its
`logs/<run>.jsonl` file. Then run `FIXTURE=fixtures/my_run.jsonl ./run.sh drive`. Mock ears re-emit only the
SessionStart/Utterance/Pause lines, never recorded claims, verdicts, interjections or the brain's synthetic
Pauses. They also start at the last `SessionStart` and cut silences longer than `FIXTURE_MAX_GAP` seconds
(default 5). A hand-written fixture needs only those three event types.
