# app/face — D (Face)

Any frontend that connects to `/ws`. `app/face/static/index.html` is served at `/` if it exists; or add a
FastAPI `router` in `routes.py` (e.g. `/tts/{id}`); or host it anywhere. Start from the reference client
`app/tools/ws_debug.html` (served at `/debug`) — it already handles Snapshot + reconnect + the audio-master
`?audio=1` rule. See `docs/INTEGRATION.md` (D section). Dev loop: `./run.sh mock` (no keys needed).
