# Contracts (schema v1.0) — source of truth: `app/schemas.py`

One wire format everywhere: a JSON object with an `event` field. A JSONL log line, a websocket message
and a `POST /publish` body are the same thing. A JSON array of events also works on `/ws` and `/publish`.
The bus fills in `seq` (order), `ts` (server time, if you didn't send one) and `src`.

## Rules that keep us flexible

- **§4 field names are frozen.** New fields are *optional with defaults* → nobody's code breaks.
- **Extra fields pass through.** Send `{"event":"Verdict", ..., "my_debug": 3}` and every consumer, the log
  and the dashboard still get it. (The bus logs a one-time note so B can make it official.)
- **New event types pass through.** `{"event":"TTSState", ...}` or anything else unknown reaches every
  subscriber untouched. Tell B if the brain should react to it.
- **Lenient where it's cheap:**
  - Links: `rel`/`relation` (also `type`/`kind`) all work.
  - Verdict labels: case and punctuation don't matter, and common fact-checker words map onto our four labels:
    - `supported`: True, Mostly true, Correct, Accurate, Supports.
    - `contradicted`: False, Mostly false, Pants on fire, Incorrect, Wrong, Inaccurate, Refutes.
    - `missing_context`: Half true, Misleading, Mixed, Needs context, Lacks context, Exaggerated, Partly false/true.
    - `unverifiable`: Unproven, Unverified, No evidence, Not enough info.
    - Any other word becomes `unverifiable`, with a one-time warning in the server log.
  - Confidence: `85`, `"85"` and `"85%"` all become `0.85`.
  - Importance: a whole number `1`, `2` or `3` (or the string `"1"`/`"2"`/`"3"`) is the 1–3 scale and becomes
    `0.33`/`0.67`/`1.0`, so `1` means *low*. `"low"`/`"medium"`/`"high"` work too. A decimal from 0 to 1 is already
    0–1, so `1.0` means *max*: in JSON `1` is an int and `1.0` is a float, so send `0.33` (not `1.0`) for low.
    Other numbers above 1 are divided by 3 and capped at `1.0`, so `2.5` becomes `0.83` and anything above 3 becomes `1.0`.
  - Timestamps: milliseconds (JS `Date.now()`) become seconds, and numeric strings work.
  - Types and nulls: numbers become strings where a string is expected (e.g. `speaker: 0` becomes `"0"`). `null`
    means "use the default" (for fields that can't be null). `is_final`/`isFinal` mean `final`.
  - `latency_ms`: floats are rounded.
  - `evidence`: bare URL strings or a single object work; `link`/`href` mean `url`.
  - Control: on the bool flags (`tts`, `director`, `web_fallback`, `interims`, `agent_speaking`, `mute`),
    `"false"`/`"0"`/`"off"` mean `false`. For `aggr`, `"4"` means `4`.
- **Times** are epoch seconds on the server clock (never phone clocks). `0` = unknown → consumers use `ts`.
  Over `/publish` and `/ws`, any `ts`/`t`/`t_start`/`t_end` more than 2 min away from the server clock (e.g.
  seconds since your audio stream started, or a laptop with the wrong time) is reset to `0`. The bus then
  stamps `ts` itself.
- **Speaker ids** are whatever A chooses (`"A"`, `"spk1"`…). Names and sides live only in `SessionStart`.

## Events

| Event | Producer → consumers | Required fields | Useful optional fields |
|---|---|---|---|
| `SessionStart` | dashboard/CLI → all | — | `motion`, `speakers{id:{name, side: prop\|opp}}`, `mode`, `brief` (organiser's debate brief: steers pre-research, shown on the stage) |
| `ResearchRequest` | stage setup screen → C | `motion` | `brief`. Researches before the debate without resetting anything; the later `SessionStart` with the same motion + brief loads the result |
| `FactSheet` | C → D | — | `motion`, `brief`, `facts[{statement, side: prop\|opp\|both, quote, url, title, number, unit}]`, each quote checked verbatim by code. Re-sent on `SessionStart`; never trimmed from the snapshot |
| `Utterance` | A → B, D | `speaker`, `text` | `id` (interims share their final's id), `t_start`, `t_end`, `final` (default true; `is_final` accepted) |
| `Pause` | A → B (Director) | `speaker` | `t` (end-of-turn detected) |
| `Claim` | B → C, D | `id`, `speaker`, `text` | `utterance_id`, `type`, `checkable`, `links[{claim_id, relation}]`, `side`, `importance` 0–1, `topic`, `t_end`, `marks` |
| `Verdict` | C → B, D | `claim_id` | `label` (supported\|contradicted\|missing_context\|unverifiable), `confidence` 0–1, `evidence[{url,title,quote}]`, `explanation`, `latency_ms`, `via`, `short_fact`, `numeric{claimed,actual,unit}`, `marks`, `guard_notes[]` (`capped` = dashboard only: the Director never voices it) |
| `SourceCard` | C → D | `claim_id`, `url` | `title`, `snippet` |
| `Interjection` | B → D (TTS) | `id`, `text` | `priority`, `claim_id`, `kind`, `target`, `evidence[0]` (show while speaking) |
| `ArgMapSnapshot` | B → D | — | full map: `nodes`, `edges`, `score{prop,opp}`, `call`, `turns`, `nonresponsive` |
| `DirectorDecision` | B → D (reasoning panel), log | — | `key`, `kind`, `action` (candidate\|held\|fired\|skipped\|dropped), `reason`, `score` |
| `DirectorStatus` | B → D (status chip), ~1 Hz | — | `floor_open`, `tts_busy`, `cooldown_left_s`, `muted`, `aggr`, `pending`, `pressure` |
| `ResearchProgress` | C → D | — | `stage` (searching\|embedding\|facts\|ready\|error), `done`, `total`, `detail`, `motion` |
| `TTSState` | D (the one `?audio=1` tab) → A, B | `speaking` | `interjection_id` — `speaking: true` means the referee is **audible now** (sent when playback actually starts, `false` once it has stopped); gate on this event only |
| `BotLine` | B (`app/bots`, `BOTS=real`) → D | `persona`, `text` | `id`, `kind` (rebut\|challenge\|concede\|nudge\|followup\|banter), `target` (the opponent's speaker id), `claim_id`, `sourced` (true only with a quoted Verdict source; a `challenge` (the claim looks wrong, or true but misrepresented) or a `concede` (a surprising truth, grudgingly granted) is AI knowledge and its text ends "(unsourced)"), `evidence`, `reply_to` (banter), `hold_s`. Text only: never spoken. The For bot (`persona: for`) is Prop's second and only addresses Opp; `against` mirrors it |
| `BotTalk` | B → D | `persona` | `line_id`, `on`. A `BotLine` opens that bot's mouth; `on: false` closes it after `hold_s`. Never a `TTSState` (that means the referee is audible) |
| `Control` | dashboard/CLI → all | `flag` | `value` — flags: `tts`, `director`, `web_fallback`, `interims`, `agent_speaking`, `mute`, `bots` (side bots may post), `force_next`, `veto`, `aggr` (1–5), `time` |
| `ModuleStatus` | supervisor/modules → D | `module` | `state` (up\|degraded\|down), `detail` (a module that returned normally, or one served only by routes.py/static, stays `up`) |

**Guardrail in the contract:** a Verdict whose label isn't `unverifiable` but has no `evidence` is
downgraded to `unverifiable` on parse. Every verdict shows its source.

**Upserts** (what the Snapshot, the reference dashboard and the argument map keep):
- A re-sent `Claim` with the same `id` replaces the old one.
- A later `Verdict` for the same `claim_id` replaces the earlier one on screen and in the map, e.g. an index
  verdict followed by a web verdict, or a `via="deep"` verdict from the background deep check about 30 s later. Spoken lines are different: the referee gives each claim at most one
  verdict line (correction, context or confirm). A better verdict that arrives after that line updates the chip
  but is not spoken. A source request ("what's your source?") doesn't count as a verdict line, so a later
  verdict can still be spoken after one.
- An interim `Utterance` is replaced by the next interim or the final with the same `id`. An interim that
  arrives *after* its final is ignored.
- A final is final. If you re-send a final with the same `id`, the transcript updates, but claims already
  extracted from it are not withdrawn. Send corrections as a new utterance.

## Examples

```json
{"event":"Utterance","id":"u-A-0007","speaker":"A","text":"Bus use outside London has fallen by a third since 2010.","t_start":1762532401.2,"t_end":1762532405.9,"final":true}
{"event":"Pause","speaker":"A","t":1762532406.8}
{"event":"Claim","id":"c12","utterance_id":"u-A-0007","speaker":"A","side":"prop","text":"Bus use outside London has fallen by a third since 2010.","type":"statistic","checkable":true,"importance":0.67,"topic":"bus use outside London","links":[],"t_end":1762532405.9}
{"event":"Verdict","claim_id":"c12","label":"missing_context","confidence":0.82,"evidence":[{"url":"https://www.gov.uk/...","title":"Bus statistics","quote":"..."}],"short_fact":"it fell 27% from 2009/10","via":"index"}
{"event":"Interjection","id":"i-0003","text":"Some context: GOV.UK notes it fell 27% from 2009/10.","kind":"context","claim_id":"c12","target":"A","priority":0.41}
```

## Latency marks (copied forward, so no joins are needed)

`speech_end` (A: `Utterance.t_end`) → `stt_final` (bus `ts` of the final) → `claim` (B) → `retrieved` (C) →
`verdict` (C; call `app.latency.finish(verdict, claim)`) → `interject` (B). `python -m app.latency logs/latest.jsonl`
prints per-stage p50/p90.
