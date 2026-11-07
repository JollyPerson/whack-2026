# core

Shared by every role. Owner: Brain (integration lead, Tong).

- `schemas.py`: the contracts from `docs/OVERVIEW.md`. Draft until issue #3 closes.
- The event bus (asyncio queues, JSONL logging, wall-clock timestamps) will live here too.

Modules talk to each other only through these types, never by importing each other.
