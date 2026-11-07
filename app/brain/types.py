"""Internal types shared by brain modules (not on the bus). Owner: B."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class Notice:
    """Argument-map finding handed to the Director (ArgMap.drain_notices() -> Director.notice()).

    kind:
      "unanswered"     - claim_id by one side has gone unanswered; `speaker` is who should answer it
      "answered"       - a previously unanswered claim got a reply; Director cancels its pending prompt
      "nonresponsive"  - `speaker`'s turn ignored the opponent's points listed in detail["ignored"]
    """
    kind: Literal["unanswered", "answered", "nonresponsive"]
    speaker: str                     # speaker addressed by a prompt (who should answer)
    t: float                         # wall-clock time the map detected it
    claim_id: Optional[str] = None
    detail: dict = field(default_factory=dict)
