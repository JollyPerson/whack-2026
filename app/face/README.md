# app/face — D (Face)

The dashboard, served at `/`. Plain HTML, CSS and JS in `static/`, with no build step and no CDN.
Dev loop: `./run.sh dev-d` (no keys needed). Protocol reference: `app/tools/ws_debug.html`, served at `/debug`.

| URL | Use |
|---|---|
| `/` | The projected view |
| `/?admin=1` | Operator controls. Keys: M mute, Esc mute, F say next now, 1–5 talkativeness, W web search, I interims, B side bots, H hide controls |
| `/?audio=1` | The one tab that speaks. Click **Enable audio** once, because browsers block speech until the page has had a click |
| `/?admin=1&audio=1` | Both, for a single demo laptop |
| `/stage` centre + final | The middle lane shows **how the sides relate**: Flip the chameleon pops up for a U-turn (Then/Now quotes, leaning towards the side that changed its story), Dove flies in from the side that conceded a point, and agreements and corrections collect under **Common ground**. **⚖ Final verdict** (or the dashboard's Final screen) switches every tab to the legal scales: one weight per claim that checked out, a cracked weight per U-turn, the beam tipped by each side's evidence score, then the summary's agreed and still-disputed points. **Back to live** returns |
| `/stage` | Opens on a **setup screen** (motion, brief, speaker names): "Research this debate" pre-researches it with a progress bar, then "Start debate" starts the session with the brief. During the debate the brief sits under the motion. Two-sided character view: each fact check pops a character onto the side that made the claim (Sunny = supported, Grumble = contradicted, Professor Hoot = missing context), with a sign and a speech bubble giving the context and source. Display only, it never speaks. Takes `?theme=light`; `?all=1` also shows unverifiable checks (Shrug the ghost). Click a past check to bring it back |
| `/mic?ch=A` or `?ch=B` | On a **phone**: the phone's browser as that debater's microphone, no app (server started with `PHONE_MICS=A,B`; `?agc=0`, `?token=`). Over USB: `adb reverse tcp:8000 tcp:8000`, then `http://localhost:8000/mic?ch=A`. See `docs/LIVE-MICS.md` section 12 |
| `/?theme=light` | Light theme, for a projector that washes out dark screens. Combines with the others |

**Silent-demo warning:** the admin view warns if the Director spoke a line and no tab said it within 3 seconds.

**Motions:** add the demo Motions to `static/motions.json`, a JSON list of strings. The picker shows them.

**Screens:** "Final screen" sends `Control(flag="screen", value="final"|"live")`, so every open tab switches together and
a refreshed tab stays on the same screen. The list always works; argument map statuses, links and the call appear
once `ArgMapSnapshot` arrives.

**Verdict (`JUDGE=real`):** the Final screen click also asks the judge (`app/judge`) for the debate's verdict. Until its
`DebateResult` arrives, the final screen shows **Judging…**, but only while the judge module runs and for at most
2 minutes. Then a card sits on top: the winner, or Draw (a narrow margin is a draw), in its side's colour with that
side's speakers; the margin; the judge's headline; both overall scores and one row per criterion (Prop – Opp); the
dance each side bot performs (♪ = with its music); and the path of the Markdown summary (the JSON is beside it).
"Decided by the scorecard formula" means Claude gave no valid answer in time, so the scorecard formula picked the
winner. A refreshed tab shows the card at once, without the reveal animation. It is hidden on the live screen, and a
new debate clears it.

**Replay fallback:** `POST /face/replay` (the button in admin) plays the recorded debate through the running pipeline.
It uses `FIXTURE`, then `fixtures/test_debate.jsonl`, then `fixtures/fake_debate.jsonl`. Use it from `./run.sh live`
with the debaters silent, because real mics stay open. In `mock`, `drive` or `dev-*` profiles, mock ears are already
playing a debate, so a replay started before that finishes overlaps it.

## Voice

The `?audio=1` tab is the only audio player. Its `say()` (`static/app.js`) plays `GET /face/tts?text=<line>`, a 24 kHz
mono WAV made on the server (`tts.py`), and falls back to browser speech if that fails (503: no server voice; 504: not
ready within `TTS_MISS_WAIT_S`; 502: every provider failed). `TTSState{speaking:true}` is sent when sound actually
starts and `false` once it has stopped; the watchdog starts at the first sound, never fires before the WAV's own end,
and cuts the audio before it says false.

| Env (`.env`) | Default | |
|---|---|---|
| `ELEVENLABS_API_KEY` | — | Turns ElevenLabs on. Server only: never logged, cached or sent to a browser |
| `ELEVENLABS_VOICE_ID` | `JBFqnCBsd6RMkjVDRZzb` | Premade "George" (British). Use a default or instant-clone voice: professional clones are slower |
| `ELEVENLABS_MODEL` | `eleven_v4_turbo` | Text to Dialogue WebSocket, one connection per line |
| `ELEVENLABS_HTTP_MODEL` | `eleven_multilingual_v2` | HTTP fallback. Not Flash v2.5, which doesn't normalise numbers |
| `ELEVENLABS_STABILITY` | `0.6` | Higher = flatter, steadier delivery |
| `TTS_PROVIDER` | `auto` | `auto` = every configured one in the order eleven → eleven_http → espeak; a name = only that one; `none` |
| `TTS_MISS_WAIT_S` | `2.5` | How long `/face/tts` waits for a line it hasn't got (the late result is still cached) |
| `TTS_PRESYNTH` | `lines` | `lines` = synthesise each Interjection as it's published; `candidates` = also the Director's candidate lines ahead of time (hides latency, ~10x the characters); `off` |

- **Credits:** a free ElevenLabs plan is 10,000 characters a month (about 125 referee lines) and asks you to credit
  ElevenLabs and keep it non-commercial. Keep `TTS_PRESYNTH=lines` on a free plan; cached lines cost nothing again.

- **Cache:** `cache/tts/<sha1(provider|model|voice|speakable text)[:20]>.wav` (gitignored). Delete the folder after changing
  voice, and before the demo if ElevenLabs failed during a rehearsal: a line cached from a fallback (espeak-ng) is reused.
- **Pre-synthesis** (`FACE=real`, i.e. `drive`, `live`, `dev-d`, `replay`): `run.py` starts synthesising every
  Interjection the moment it is published (and the Director's candidates too with `TTS_PRESYNTH=candidates`). With
  `FACE=off` the routes still work, synthesising on first request.
- **Numbers:** `speakable()` rewrites the audio only ("£3bn" → "3 billion pounds", "27%" → "27 percent",
  "2009/10" → "2009 to 2010"). The banner shows the exact text.
- **No key:** `espeak-ng` on the server if it is installed (`pacman -S espeak-ng`), else the browser's speech.
  Firefox on Linux needs `speech-dispatcher` for that.
- **Check it:** `curl -o line.wav 'localhost:8000/face/tts?text=Point%20of%20fact.' && pw-play line.wav`.

## Mate Engine avatar (optional)

A Mate Engine desktop avatar can show the referee's line in a speech bubble and move its mouth while the voice plays.
It long-polls `GET /face/mate/next` (protocol: `docs/INTEGRATION.md`, D section) and only reads: if it crashes,
the voice and the dashboard carry on. With `FACE=real` a `mate` dot goes green on its first poll and red 5 s after
its last one. Dances are operator-only: `curl -XPOST localhost:8000/face/mate/dance -d '{"title":""}' -H
'content-type: application/json'` and `curl -XPOST localhost:8000/face/mate/dance/stop`. Dance music reaches the
mics, so only dance outside the live debate. While the avatar is polling, Ctrl-C waits for its open poll (up to its
`wait`, 10 s) before the server exits: press Ctrl-C twice to stop at once.

**Side bots (`BOTS=real`).** Two more avatars can show the For bot (Prop's second) and the Against bot (Opp's second).
Each Mate Engine instance polls its own channel, set with `pollPath` in its own `speech_bridge.json`:
`/face/mate/next/for` or `/face/mate/next/against` (`/face/mate/next/referee` is the route above; any other channel is
a 404). A bot's channel carries only its own `BotLine`s, as a text-only bubble with the mouth open, and `BotTalk`
closes the mouth after the line's `hold_s`. `SessionStart`, mute and the `bots` flag going off clear its bubble. The
referee's lines never reach a bot's channel, and bot lines never reach the referee's. Each channel has its own dot
(`mate_for`, `mate_against`), shown only once that avatar has polled. Dances take an optional `"channel"`:
`-d '{"title":"","channel":"for"}'`; without one (or `""`), every avatar dances, and `dance/stop` likewise.

The dashboard shows a **For / Against strip** above the panels with each bot's latest line, in its side's colour. An
unsourced `challenge` or `concede` (Claude's own knowledge) is tagged **AI knowledge**, and a sourced `rebut` names
its source. The strip stays hidden until a bot has spoken, so nothing changes with the bots off, and it is not shown
on the final screen. In `?admin=1`, **Side bots on/off** (key B) sends `Control(flag="bots")`: off stops new lines
and clears the bots' bubbles. The button appears only when the `bots` module runs or a bot line has arrived.

**Verdict dances.** A `DebateResult` makes both side bots dance (the referee's avatar doesn't). The winner's bot dances
`JUDGE_DANCE_WIN` (default `MMD-Conqueror`) with its music, and the loser's bot dances `JUDGE_DANCE_LOSE`
(`MMD-Lost One_s Weeping`) with no sound. On a draw both dance `JUDGE_DANCE_DRAW` (`MMD-皆大欢喜-卜逻desu`), silently with
`JUDGE_MUSIC=winner`. Mate Engine matches each title to a dance file's name in its CustomDances, ignoring case; a
title it can't find plays its first dance. Each dance stops after `JUDGE_DANCE_S` (30 s), sent as the command's
`seconds`, or at the next `SessionStart`, which sends `dance_stop` to both bots. While a bot dances, its `BotLine`s
don't reach its avatar, because a `say` stops the dance in Mate Engine. The operator route takes the same switches,
which is handy for a rehearsal: `-d '{"title":"MMD-Conqueror","channel":"for","on":true,"seconds":30}'` (`on` =
with music, `seconds` 0 or absent = to the end). A Mate Engine build that predates `on` and `seconds` ignores both: it
plays at its configured volume to the end. Mixamo "Victory" and "Defeated" animations are a later upgrade, not built.
