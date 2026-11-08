# app/evidence — C (Evidence)

Owner: Harkaran (`JollyPerson`), issue #15. Consumes `Claim` and `SessionStart`. Produces `Verdict`, `SourceCard` and `ResearchProgress`.

| File | What it does |
|---|---|
| `run.py` | Checks each Claim against the index, then the web, and publishes the result |
| `deep.py` | The background deep check, which can publish a second Verdict about 30 s later |
| `cache.py` | The verdict cache: a claim already settled on the Motion gets the same Verdict at once |
| `checks.py` | Quote verification, numeric comparison and the confidence cap |
| `verdicts.py` | The verdict prompt and schema |
| `index.py` | Chunks, BM25 and optional vectors |
| `preresearch.py` | Builds and saves a Motion's index |
| `search.py` | Exa web search |
| `llm.py` | The verdict model |

## Setup

Add to `.env`:

- `EXA_API_KEY`: needed for pre-research and the web fallback.
- `EVIDENCE_ANTHROPIC_API_KEY`: keeps Evidence separate from Brain's rate limits. Without it, Evidence uses Brain's LLM.
- `EVIDENCE_MODEL`: optional. Defaults to `claude-opus-5-5` for accuracy (claim extraction stays on `LLM_MODEL`).
- `EVIDENCE_EMBEDDER`: optional, one of `fastembed`, `openai` or `none`.

Embeddings are optional, but they're what makes a big index pay off. `fastembed` (local `BAAI/bge-small-en-v1.5`) installs on Python 3.12, which the server image uses. It has no wheels for Python 3.14, so `requirements/evidence.txt` skips it there and retrieval runs on BM25 alone. For local work with embeddings, make a 3.12 venv with uv:

```bash
uv venv --seed --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
```

`openai` uses `EVIDENCE_OPENAI_API_KEY` or `OPENAI_API_KEY`. The model downloads once into `cache/fastembed` (`EVIDENCE_EMBED_CACHE`).

What the embedder changes, from the verdict eval (Opus, index only, 3 runs each, out of 20):

| Build | Retrieval | Score |
|---|---|---|
| 20 queries, about 180 pages per Motion | BM25, 5 chunks | 17, 17, 17 |
| 40 queries plus a primary-source pass, about 600 pages | BM25, 5 chunks | 13, 12, 14 |
| the same 600 pages | BM25 + vectors, 8 chunks | 18, 18, 18 |

The holdout Motion went from 1/5 to 3/5 on the last setup. More pages only help when vectors rank them, so a build picks its size from the embedder: with one, 40 planned queries each run twice (open, and within `PRIMARY_DOMAINS` in `preresearch.py`), 8,000 characters a page; without one, the original 20 queries. Retrieval shows the verdict model 8 chunks when it has vectors and 5 when it doesn't. The `PRERESEARCH_*` and `EVIDENCE_TOP_K*` variables override these.

## Other models

Evidence uses whatever `make_llm()` builds when there's no Anthropic key for it, so any OpenAI-compatible server works through `LLM_PROVIDER=openai`, `LLM_BASE_URL`, `LLM_API_KEY` and `OPENAI_MODEL`. That switches claim extraction too. Kimi K2.6 on Canopy Wave answers a verdict in about 2.5 s, but its key allows 1 request a second and 6 a minute: enough for the eval with `EVAL_LLM_PER_MINUTE=5.5`, which spaces calls and retries on HTTP 429, and not enough for a live debate.

## Verdict cache

A claim already settled on the Motion, in this debate or an earlier one, is answered at once with the stored Verdict (`via="cache"`), without searching or calling the model. That makes repeats instant and gives the same claim the same Verdict every time.

- **Storage:** one JSON-lines file per Motion in `cache/verdicts/` (`EVIDENCE_VERDICT_CACHE_DIR`). On the server that's `/srv/cache/verdicts` in the `cache` volume, so it survives restarts and deploys.
- **Matching:** a claim matches a stored one when it has exactly the same figures and shares at least 70% of its words, the argument map's duplicate threshold. "Fell 27%" never reuses a verdict for "fell 30%", and "did not fall" doesn't match "fell".
- **What's kept:** only Verdicts worth repeating: not unverifiable, with a quote the guard verified, and confidence of at least 0.6 (`EVIDENCE_CACHE_MIN_CONF`). A later, better Verdict for the same claim, such as a deep check's, replaces the stored one.
- **Forgetting:** delete the Motion's file to drop a wrong verdict. The operator can turn the cache off with the `verdict_cache` flag.

A wrong verdict that gets stored is repeated until its file is deleted, so check the cache after a test debate.

## Deep check

Off by default: on Kimi K2.6 it fixed no verdicts and broke one, and it is untested on Opus. Turn it on with the `deep_check` flag (`/control`), after `python -m eval.eval_verdicts --layer deep` shows it fixing more than it breaks. When on, each checkable claim with importance of at least 0.5 gets a second check in the background, with up to 30 s:

1. The fast verdict model rewrites the spoken claim into up to 4 written search queries, naming the report or dataset likely to settle it, with figures written the way documents write them.
2. Exa searches each query twice with full text (12,000 characters a page): once within `PRIMARY_DOMAINS` and once on the open web. The Motion's index is searched again with the written queries too.
3. The 10 best passages, ranked by BM25 fused across the claim and every query, go to the verdict model at medium effort.

It publishes a Verdict with `via="deep"` only when it does better: it has a verdict where the fast check had none, it agrees and is surer, or it disagrees and is at least as sure. Not finding a source again never undoes a verdict whose quote was checked. A later Verdict replaces the earlier one on screen and in the map, and the Director can still speak it if nothing was said about that claim yet and its TTL (45 s for a correction) hasn't run out.

Two run at a time, and claims beyond 6 waiting are skipped, so a burst of claims can't build a backlog that makes every update late. The operator can turn it off with the `deep_check` flag. Settings: `EVIDENCE_DEEP_TIMEOUT_S`, `_CONCURRENCY`, `_MAX_WAITING`, `_MIN_IMPORTANCE` and `_EFFORT`. Measure it with `python -m eval.eval_verdicts --layer deep`, which also reports which verdicts it fixed and which it broke.

## Research a new Motion on the fly

The stage's setup screen (`/stage` → New debate) takes a Motion and a brief and sends a `ResearchRequest`. Evidence
plans its search queries from both, searches, indexes, then pulls out a **fact sheet**: up to 40 short facts, each
tagged with the side it helps, kept only if its quote is verbatim in the source. Both are saved in `cache/evidence/`,
keyed by Motion + brief, and the `SessionStart` that follows loads them (or joins the build if it's still running).
Checks put the matching facts in front of the index results. Progress arrives as `ResearchProgress` with `motion` set,
the facts as a `FactSheet`. `PREPARE_DEADLINE_S` (default 150) caps the search; a debate started without preparing
still researches live with 90 s. The brief steers *what* is searched, never the verdict: the judge only sees sources.

From the command line: `.venv/bin/python -m app.evidence.preresearch "This house would…" --brief "…"`. Add fact sheets
to indexes built before fact sheets existed with `--all --facts-only`.

## Build the demo indexes in advance

The demo Motions are in `motions.json`. The four marked `eval` go in the eval set, and the `holdout` one is the unseen check.

```bash
.venv/bin/python -m app.evidence.preresearch --all --missing
```

Indexes are saved in `cache/evidence/`, which is gitignored, so build them wherever the server runs and keep that folder between deploys. On the Vultr server each deploy does this by itself once `EXA_API_KEY` is in `app.env`, into the `cache` volume, and logs to `/srv/logs/preresearch.log`. See [docs/DEPLOY.md](../../docs/DEPLOY.md). `--missing` skips Motions already built, so it's safe to run on every deploy, and the eval's indexes don't change underneath it. To rebuild a Motion, delete its files first. Set `EVIDENCE_INDEX_DIR` to use another folder.

Embedding a big build takes about 10 minutes per Motion on a 6-core laptop, and much longer on the server's single vCPU, which also has only 2 GB of memory. Build them on a laptop and copy `cache/evidence/` into the server's `cache` volume, so the deploy's `--missing` step finds them and skips the build.

Run `./run.sh dev-c` to develop with mock speech and claims.
