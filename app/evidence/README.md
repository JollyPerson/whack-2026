# app/evidence — C (Evidence)

Consume `Claim` (+ `SessionStart` for pre-research), produce `Verdict`, `SourceCard`, `ResearchProgress`;
see `docs/INTEGRATION.md` (C section). Put `async def run(bus)` in `run.py`, or run externally with
`EVIDENCE=external` and talk over `/ws`. Until then `mock_evidence` answers with canned verdicts.
Inject a single claim for testing: `curl -X POST localhost:8000/publish -H 'content-type: application/json' -d @claim.json`.
Dependencies: `requirements/evidence.txt`. Dev loop: `./run.sh dev-c`.
