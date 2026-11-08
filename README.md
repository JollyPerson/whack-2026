# debate-ref — live debate fact-checker + moderator

Project context, roles and the demo plan: [`docs/OVERVIEW.md`](docs/OVERVIEW.md).

WHACK hackathon, incident.io track. Speech in → claims → evidence-backed verdicts → a referee that
interjects at pauses. Plan: [`docs/B-PLAN.md`](docs/B-PLAN.md). Contracts: [`docs/CONTRACTS.md`](docs/CONTRACTS.md).
How to plug your part in: [`docs/INTEGRATION.md`](docs/INTEGRATION.md).

## Run it

```bash
./run.sh mock                      # whole pipeline on mocks, zero API keys -> open http://localhost:8000/debug
./run.sh drive                     # recorded debate (fixtures/) through the REAL brain + evidence
./run.sh live                      # real mics
./run.sh replay --file logs/latest.jsonl   # demo backup: replays the last recorded run, no network
PRINT=1 ./run.sh mock              # also print one line per event in the terminal
./run.sh test-haiku                # the real pipeline on Haiku everywhere (claims, verdicts, consistency, summary)
                                   # replaying fixtures/consistency_debate.jsonl. COSTS TOKENS: open /stage, then
                                   # "⚖ Final verdict" at the end. Usage per model is in the log and the status dots
FIXTURE=fixtures/consistency_debate.jsonl ./run.sh mock   # the same debate, free: simulated findings + summary
./check.sh                         # tests (run before pushing)
```

Pushes to `master` deploy to Vultr: see [`docs/DEPLOY.md`](docs/DEPLOY.md).

Replay options: `--speed 2` (2x faster), `--max-gap 5` (silences longer than 5 s are cut to 5 s; this is the
default, `0` keeps them), `--all-sessions` (by default a log replays from its last `SessionStart`, so rehearsals
earlier in the same log are skipped), `--only Utterance Pause` (re-drive the live pipeline from recorded speech).
`logs/latest.jsonl` always points at the last run that wasn't itself a replay. A missing `--file` stops with an
error that says how to create it. `run.sh` reinstalls packages whenever any `requirements*.txt` file changes. If pip fails (offline),
it warns and starts anyway with the existing `.venv`. `SKIP_PIP=1` never runs pip.

Mix and match roles per run with env vars — each is `real | mock | off | external | pkg.module:function`:

```bash
EARS=real BRAIN=real EVIDENCE=mock ./run.sh mock     # A's ears + B's brain, canned verdicts
EVIDENCE=external ./run.sh drive                     # C runs as a separate process (connects to /ws)
SPEED=4 ./run.sh drive                               # replay the fixture 4x faster
```

| Profile | ears | brain | evidence | face |
|---|---|---|---|---|
| `mock` | mock | mock | mock | off |
| `drive` | mock (fixture) | real | real | real |
| `live` | real | real | real | real |
| `dev-a` | real | off | off | off |
| `dev-b` | mock | real | mock | off |
| `dev-c` | mock | mock | real | off |
| `dev-d` | mock | mock | mock | real |
| `replay` | off | off | off | real (+ `--file`) |

Override values: `real`, `mock`, `off`, `external`, `pkg.module:function` (or `pkg.module`, meaning
`pkg.module:run`). Case doesn't matter. A typo such as `EVIDENCE=Externl` shows a red dot that says so. There is
no face mock, so use `FACE=off` or `FACE=external`.

Keys go in `.env` (copy `.env.example`, which must hold key *names* only; `.env` is gitignored, `.env.example` is
not). `run.sh` loads `.env`, and so does `app/config.py` on import, so `python -m ...` and `scripts/` see the keys
too. Tests never load it. The claim extractor uses **Claude** (`ANTHROPIC_API_KEY`, default
`claude-haiku-5-5`) or **ChatGPT** (`OPENAI_API_KEY`, default `gpt-6-luna`), whichever key is present. If both are
present Claude wins, unless `OPENAI_MODEL` is set. Any OpenAI-compatible server works via `LLM_BASE_URL`.
`LLM_PROVIDER=heuristic` forces the no-LLM path, and with no key it also runs on heuristics.
Check speed with `.venv/bin/python scripts/bench_llm.py` and quality with `.venv/bin/python -m eval.eval_extract`.

## Side bots

Two seconds heckle the opposing debater in text speech bubbles (two Mate Engine avatars, see
[`docs/INTEGRATION.md`](docs/INTEGRATION.md) "Channels (side bots)"): **For** is Prop's second and only answers Opp's
claims, **Against** mirrors it. No profile starts them; add `BOTS=real` to any run:

```bash
BOTS=real ./run.sh live                    # or drive, mock, ...
```

- A sourced Verdict becomes a `rebut` that names its source ("The ONS says 4.2%, not 6%. Try again."). Without one,
  Claude checks the claim from its own knowledge and, only when confident (`BOTS_CHECK_MIN_CONF`, 0.80), posts a
  line ending "(unsourced)": a `challenge` when the claim looks wrong ("6%? Last I checked, it was nearer 4%.") or
  true but misrepresented ("True, but that's against a modelled baseline."), or, rarely, a grudging `concede` when
  it is true but widely believed false ("Annoyingly, that one checks out."). At most one of these per claim, and
  never against the claim's Verdict. Otherwise a `followup` question. Every check result is logged at INFO
  ("bots: check c7: misleading 0.79 -> not fired: below 0.80"), to tune the bar at rehearsal.
- Unanswered points get a `nudge`. The bots trade a dry `banter` line at most every `BOTS_BANTER_GAP_S` (20 s),
  only while nothing more important is running or queued for either bot (`BOTS_BANTER=0` turns it off).
  Priority: rebut > challenge > concede > nudge > followup > banter.
- Each bot posts at most one bubble every `BOTS_MIN_GAP_S` (10 s). The admin flag `bots` pauses both; mute hides
  everything. Tunables are the `BOTS_*` names in `app/config.py` and `.env.example`.
- They use the same Anthropic key as the brain (`BOTS_MODEL` for follow-ups and banter, `BOTS_CHECK_MODEL` for the
  check). With no LLM they still rebut, nudge and ask template follow-ups, but never challenge, concede or banter.

## Layout and ownership

```
app/schemas.py   B  event contracts (additive changes only — ask B)
app/bus.py       B  pub/sub + JSONL log + replay
app/main.py      B  module registry, /ws, /publish, /session, /control, /health, /debug
app/config.py    B  flags + tunables (all env-overridable)
app/brain/       B  claims.py (+llm.py, prompts.py), argmap.py, director.py (+voice.py), run.py
app/bots/        B  side bots (BOTS=real): lines.py (wording, guards), pacer.py, writer.py (LLM), run.py
app/ears/        A  run.py with `run(bus)` (async or plain) and/or routes.py (FastAPI router) — or external
app/evidence/    C  run.py with `run(bus)` (async or plain) — or external
app/face/        D  static/index.html (served at /), routes.py, run.py — or any frontend on /ws
app/mocks/       B  mock_ears / mock_brain / mock_evidence / printer
app/tools/       B  ws_debug.html (reference client, served at /debug), tail.py
app/latency.py   B  latency report + helpers
ears/            A  Thomas's uv package, until it moves into app/ears/
fixtures/        shared test debates (JSONL, same format as logs)
eval/            extraction eval and the 20-claim verdict eval
logs/            one JSONL per run (+ latest.jsonl) — every event, replayable
deploy/          production stack on Vultr (see docs/DEPLOY.md)
```

**Rules:** edit only your own folder and your own `requirements/<role>.txt`. Shared files change through B
(turnaround < 10 min). Need a new field? Just send it — extra fields pass through everywhere — and tell B
if it should become official. Every run is logged; `./run.sh replay --file logs/<run>.jsonl` reproduces it.

## Fallback ladder (demo)

L0 live mics → L1 `./run.sh drive` (recorded debate, live pipeline) → L2 `./run.sh replay --file fixtures/golden_run.jsonl`
(no network) → L3 backup video. Keep the next command typed in a second terminal.

**`fixtures/golden_run.jsonl` is not in the repo.** It exists only after you save a good run at the 20:00
checkpoint with `cp logs/latest.jsonl fixtures/golden_run.jsonl` (cp follows the symlink). Until then L2 stops at
once with "No such file" and that same `cp` hint, so check `ls fixtures/golden_run.jsonl` before judging. A raw
log from a live rehearsal works as it is: the replay starts at its last `SessionStart` and cuts idle gaps. The
same log also works as a `drive` fixture (`FIXTURE=fixtures/golden_run.jsonl ./run.sh drive`). In that case mock
ears re-emit only its SessionStart/Utterance/Pause lines and the live brain does the rest.
