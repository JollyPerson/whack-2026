# app/evidence — C (Evidence)

Owner: Harkaran (`JollyPerson`), issue #15. Consumes `Claim` and `SessionStart`. Produces `Verdict`, `SourceCard` and `ResearchProgress`.

| File | What it does |
|---|---|
| `run.py` | Checks each Claim against the index, then the web, and publishes the result |
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
- `EVIDENCE_MODEL`: optional. Defaults to `LLM_MODEL`.
- `EVIDENCE_EMBEDDER`: optional, one of `fastembed`, `openai` or `none`.

Embeddings are optional. `fastembed` isn't in the requirements because it doesn't install on Python 3.14, and a failed install would affect everyone's `run.sh`. It needs an older Python. `openai` uses `EVIDENCE_OPENAI_API_KEY` or `OPENAI_API_KEY`. With neither, retrieval runs on BM25 alone.

## Build the demo indexes in advance

The demo Motions are in `motions.json`. The four marked `eval` go in the eval set, and the `holdout` one is the unseen check.

```bash
.venv/bin/python -m app.evidence.preresearch --all --missing
```

Indexes are saved in `cache/evidence/`, which is gitignored, so build them wherever the server runs and keep that folder between deploys. `--missing` skips Motions already built, so it's safe to run on every deploy, and the eval's indexes don't change underneath it. To rebuild a Motion, delete its files first. Set `EVIDENCE_INDEX_DIR` to use another folder.

Run `./run.sh dev-c` to develop with mock speech and claims.
