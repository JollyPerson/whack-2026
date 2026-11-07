"""Shared contracts. DRAFT, not frozen: see issue #3. Owned by Brain (integration lead).

Mirrors the "Shared contracts" section of docs/OVERVIEW.md. Don't change fields
without raising it with the integration lead; every module depends on these.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Utterance:
    id: str
    speaker: str
    text: str
    t_start: float
    t_end: float
    final: bool


@dataclass
class Pause:
    speaker: str
    t: float


@dataclass
class ClaimLink:
    claim_id: str
    kind: Literal["supports", "attacks"]


@dataclass
class Claim:
    id: str
    utterance_id: str
    speaker: str
    text: str
    type: str
    checkable: bool
    links: list[ClaimLink] = field(default_factory=list)


@dataclass
class Evidence:
    url: str
    title: str
    quote: str


@dataclass
class Verdict:
    claim_id: str
    label: Literal["supported", "contradicted", "missing_context", "unverifiable"]
    confidence: float
    evidence: list[Evidence]
    explanation: str
    latency_ms: int


@dataclass
class SourceCard:
    claim_id: str
    url: str
    title: str
    snippet: str


@dataclass
class Interjection:
    id: str
    text: str
    priority: int
    claim_id: str
