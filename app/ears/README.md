# app/ears — A (Ears)

Produce `Utterance` (+ interims) and `Pause` events; see `docs/INTEGRATION.md` (A section) and `docs/CONTRACTS.md`.
Put `async def run(bus)` in `run.py` and/or a FastAPI `router` in `routes.py` (e.g. `/mic?ch=A` phone page),
or run externally and publish over `/ws` / `POST /publish`. Until then `mock_ears` replays `fixtures/`.
Dependencies: `requirements/ears.txt`. Dev loop: `PRINT=1 EARS=real ./run.sh dev-a`.
