# app/face — D (Face)

The dashboard, served at `/`. Plain HTML, CSS and JS in `static/`, with no build step and no CDN.
Dev loop: `./run.sh dev-d` (no keys needed). Protocol reference: `app/tools/ws_debug.html`, served at `/debug`.

| URL | Use |
|---|---|
| `/` | The projected view |
| `/?admin=1` | Operator controls. Keys: M mute, Esc mute, F say next now, 1–5 talkativeness, W web search, I interims, H hide controls |
| `/?audio=1` | The one tab that speaks. Click **Enable audio** once, because browsers block speech until the page has had a click |
| `/?admin=1&audio=1` | Both, for a single demo laptop |
| `/?theme=light` | Light theme, for a projector that washes out dark screens. Combines with the others |

**Silent-demo warning:** the admin view warns if the Director spoke a line and no tab said it within 3 seconds.

**Motions:** add the demo Motions to `static/motions.json`, a JSON list of strings. The picker shows them.

**Screens:** "Final screen" sends `Control(flag="screen", value="final"|"live")`, so every open tab switches together and
a refreshed tab stays on the same screen. The list always works; argument map statuses, links and the call appear
once `ArgMapSnapshot` arrives.

**Replay fallback:** `POST /face/replay` (the button in admin) plays the recorded debate through the running pipeline.
It uses `FIXTURE`, then `fixtures/test_debate.jsonl`, then `fixtures/fake_debate.jsonl`. Use it from `./run.sh live`
with the debaters silent, because real mics stay open. In `mock`, `drive` or `dev-*` profiles, mock ears are already
playing a debate, so a replay started before that finishes overlaps it.

**Voice:** browser speech for now, so it works with no key and no network. To use a hosted voice, replace `say()` in
`static/app.js`, which must call `done()` once when playback ends and return a function that cuts it off.
