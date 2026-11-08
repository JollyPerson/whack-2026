"""Shared event contracts (handoff §4). Owner: B (Brain + integration lead).

Rules
- §4 field names are frozen. Changes after the freeze are ADDITIVE: new optional fields with defaults.
- Built to bend, because A/C/D are still designing their parts:
  * every event keeps unknown fields (extra="allow"), so a teammate can attach data without asking B;
  * parse_event() turns an unknown `event` name into a plain Event instead of failing, so a teammate
    can introduce a whole new event type (e.g. "TTSState") without touching this file;
  * common spelling variants are accepted (Link "rel", verdict "Mostly false", ms timestamps, is_final);
  * cheap coercions: numbers -> str ids/speakers, numeric strings -> numbers, null -> the field's default,
    bare URL strings -> Evidence, float latency_ms -> int, "85"/"85%" confidence -> 0.85,
    importance 1/2/3 or "1"/"2"/"3" (the LLM's 1..3) -> 0.33/0.67/1.0, but 1.0 (a float) is already 0..1.
- The discriminator is `event` (not `type`: Claim.type is a §4 field).
- Times are wall-clock epoch seconds (time.time()), stamped on the server. Milliseconds (JS Date.now())
  are converted automatically. 0 means "unknown": consumers fall back to the envelope `ts`.
- IDs: Utterance "u-<speaker>-<n>" (interim revisions share the id of their final), Claim "c<n>",
  Interjection "i-<n>". One wire format: a JSONL line == a websocket message == ev.model_dump_json().
"""
from __future__ import annotations

import itertools
import logging
import re
from typing import Annotated, Any, Literal, Optional, Union, get_args

from pydantic import (AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter,
                      ValidationError, field_validator, model_validator)

SCHEMA_VERSION = "1.0"
log = logging.getLogger("schemas")


def _num(v: Any) -> Any:
    """'1.5' -> 1.5; anything else unchanged (pydantic then accepts or rejects it)."""
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            pass
    return v


def _secs(v: Any) -> Any:
    """Accept epoch seconds or epoch milliseconds (numbers or numeric strings)."""
    v = _num(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e11:
        return v / 1000.0
    return v


def _unit(v: Any) -> Any:
    """0..1 scores: 0.85, 85, "85" and "85%" all mean 0.85. Out of range is clamped."""
    pct = isinstance(v, str) and v.strip().endswith("%")
    v = _num(v.strip().rstrip("%") if isinstance(v, str) else v)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return min(max(v / 100.0 if pct or v > 1 else v, 0.0), 1.0)
    return v


_IMP_SCALE = {"1": 0.33, "2": 0.67, "3": 1.0, "low": 0.33, "medium": 0.67, "high": 1.0}


def _importance(v: Any) -> Any:
    """0..1. The 1..3 scale is an int 1/2/3 or the string "1"/"2"/"3" (also low/medium/high) -> 0.33/0.67/1.0,
    so int 1 is low but float 1.0 (JSON `1.0`) is already 0..1 = max. Other numbers: <= 1 as is (clamped at 0),
    > 1 scaled x/3 and capped at 1.0 (so 2.5 -> 0.83 and anything > 3 -> 1.0)."""
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 3:
        return _IMP_SCALE[str(v)]
    if isinstance(v, str) and v.strip().lower() in _IMP_SCALE:
        return _IMP_SCALE[v.strip().lower()]
    v = _num(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return min(v / 3.0, 1.0) if v > 1 else max(v, 0.0)
    return v


def _int(v: Any) -> Any:
    v = _num(v)
    return round(v) if isinstance(v, float) else v


def _evidence(v: Any) -> Any:
    """Evidence may arrive as one item, and an item may be a bare URL string."""
    if isinstance(v, (str, dict)):
        v = [v]
    return [{"url": e} if isinstance(e, str) else e for e in v] if isinstance(v, list) else v


Epoch = Annotated[float, BeforeValidator(_secs)]
Unit = Annotated[float, BeforeValidator(_unit)]
Speaker = str          # channel id chosen by A ("A"/"B", "spk1", ...). Names/sides live in SessionStart.
Marks = dict[str, Epoch]   # latency trace: stage name -> epoch seconds, copied forward stage to stage

_uid = itertools.count(1)


_NOT_NULL: dict[type, frozenset] = {}


def _not_nullable(cls: type) -> frozenset:
    """Input names (incl. aliases) of fields that reject None; cached per model class."""
    if cls not in _NOT_NULL:
        names = set()
        for n, f in cls.model_fields.items():
            if f.annotation is Any or type(None) in get_args(f.annotation):
                continue
            names.add(n)
            alias = f.validation_alias
            names.update(c for c in getattr(alias, "choices", [alias]) if isinstance(c, str))
        _NOT_NULL[cls] = frozenset(names)
    return _NOT_NULL[cls]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True, coerce_numbers_to_str=True)

    @model_validator(mode="before")
    @classmethod
    def _null_means_default(cls, d: Any) -> Any:
        """{"t_start": null} == field omitted (fields that accept None keep it)."""
        if isinstance(d, dict) and any(v is None for v in d.values()):
            nn = _not_nullable(cls)
            d = {k: v for k, v in d.items() if v is not None or k not in nn}
        return d


class Event(_Model):
    """Envelope shared by every event."""
    event: str
    seq: int = 0          # set by Bus.publish (monotonic per process)
    ts: Epoch = 0.0       # emit time, set by Bus.publish if 0
    src: str = ""         # producing module: "ears" | "mock_ears" | "brain" | "dashboard" | ...


# ----------------------------------------------------------------------------- §4 contracts
class Utterance(Event):
    event: Literal["Utterance"] = "Utterance"
    id: str = Field(default_factory=lambda: f"u-{next(_uid):04d}")
    speaker: Speaker
    text: str
    t_start: Epoch = 0.0  # speech start
    t_end: Epoch = 0.0    # speech end (best estimate)
    final: bool = Field(True, validation_alias=AliasChoices("final", "is_final", "isFinal"))


class Pause(Event):
    event: Literal["Pause"] = "Pause"
    speaker: Speaker      # who just stopped talking
    t: Epoch = 0.0        # when end-of-turn silence was detected


class Link(_Model):
    claim_id: str
    relation: Literal["supports", "attacks"] = Field(
        validation_alias=AliasChoices("relation", "rel", "type", "kind"))


class Claim(Event):
    event: Literal["Claim"] = "Claim"
    id: str
    utterance_id: str = ""
    speaker: Speaker
    text: str
    # known values: statistic, comparison, causal, attribution, fact, prediction, value, other
    type: str = "other"
    checkable: bool = True
    links: list[Link] = Field(default_factory=list)
    # additions (optional) -----------------------------------------------------------------
    side: Optional[str] = None     # "prop" | "opp" (from SessionStart.speakers)
    importance: Annotated[float, BeforeValidator(_importance)] = 0.5   # 0..1 (int 1..3 -> 0.33..1.0): Director, C priority
    topic: str = ""                # <= 4 words, for the moderator's voice ("bus use outside London")
    canonical_key: Optional[str] = None   # normalised subject+numbers; same claim said twice -> same key
    t_end: Epoch = 0.0             # speech end of the source utterance (copied from Utterance.t_end)
    source: str = "llm"            # "llm" | "heuristic" | "mock"
    marks: Marks = Field(default_factory=dict)


class Evidence(_Model):
    url: str = Field(validation_alias=AliasChoices("url", "link", "href"))
    title: str = ""
    quote: str = ""                # MUST be verbatim in the fetched source (C verifies)


EvidenceList = Annotated[list[Evidence], BeforeValidator(_evidence)]


class Numeric(_Model):
    claimed: Optional[float] = None
    actual: Optional[float] = None
    unit: str = ""                 # "%", "GBP", "people", ...


VERDICT_LABELS = ("supported", "contradicted", "missing_context", "unverifiable")
# Fact-checker vocabularies (PolitiFact, Google Fact Check textualRating, FEVER, ...) -> our four labels.
# Keys are normalised: lower case, every run of non-alphanumerics -> "_" ("Pants on Fire!" -> pants_on_fire).
_LABEL_ALIASES = {
    **dict.fromkeys(("true", "mostly_true", "correct", "mostly_correct", "accurate", "mostly_accurate",
                     "supports", "support", "verified", "confirmed"), "supported"),
    **dict.fromkeys(("false", "mostly_false", "pants_on_fire", "incorrect", "mostly_incorrect", "wrong",
                     "inaccurate", "mostly_inaccurate", "refutes", "refuted", "contradicts", "debunked", "fake"),
                    "contradicted"),
    **dict.fromkeys(("half_true", "half_truth", "misleading", "mixed", "mixture", "needs_context", "lacks_context",
                     "context", "exaggerated", "partly_false", "partly_true", "partially_false", "partially_true",
                     "cherry_picked", "out_of_context"), "missing_context"),
    **dict.fromkeys(("unproven", "unverified", "no_evidence", "unknown", "not_enough_info",
                     "not_enough_information", "insufficient_evidence", "cannot_verify", "can_t_verify"),
                    "unverifiable"),
}
_warned_labels: set[str] = set()


def norm_label(v: Any) -> Any:
    """'Mostly false' -> contradicted, 'Half-True' -> missing_context, ... Unknown -> unverifiable (warned once)."""
    if not isinstance(v, str):
        return v
    k = re.sub(r"[^a-z0-9]+", "_", v.lower()).strip("_")
    k = _LABEL_ALIASES.get(k, k)
    if k not in VERDICT_LABELS:
        if v not in _warned_labels:
            _warned_labels.add(v)
            log.warning("unknown verdict label %r -> unverifiable (use %s)", v, "|".join(VERDICT_LABELS))
        k = "unverifiable"
    return k


class Verdict(Event):
    event: Literal["Verdict"] = "Verdict"
    claim_id: str
    label: Literal["supported", "contradicted", "missing_context", "unverifiable"] = "unverifiable"
    confidence: Unit = 0.0         # 0..1 (percentages are converted)
    evidence: EvidenceList = Field(default_factory=list)
    explanation: str = ""
    latency_ms: Annotated[int, BeforeValidator(_int)] = 0   # verdict emit - speech end of the source utterance
    # additions (optional) -----------------------------------------------------------------
    via: str = "none"              # "index" | "web" | "cache" | "timeout" | "none" | ...
    short_fact: str = ""           # <= 10 words the moderator can say ("unemployment was 4.2%")
    numeric: Optional[Numeric] = None
    marks: Marks = Field(default_factory=dict)

    @field_validator("label", mode="before")
    @classmethod
    def _norm_label(cls, v: Any) -> Any:
        return norm_label(v)

    @model_validator(mode="after")
    def _no_source_no_verdict(self):
        # Guardrail written into the contract: every verdict shows its source.
        if self.label != "unverifiable" and not self.evidence:
            self.label, self.confidence = "unverifiable", 0.0
        return self


class SourceCard(Event):
    event: Literal["SourceCard"] = "SourceCard"
    claim_id: str
    url: str
    title: str = ""
    snippet: str = ""


class Interjection(Event):
    event: Literal["Interjection"] = "Interjection"
    id: str
    text: str                      # exactly what TTS says (<= ~15 words)
    priority: float = 0.5          # higher = more urgent
    claim_id: Optional[str] = None   # None for moderator prompts ("Opposition, you haven't answered...")
    # additions (optional) -----------------------------------------------------------------
    kind: str = "correction"       # correction | context | confirm | source_req | prompt | time | dry
    target: Optional[str] = None   # speaker id addressed
    evidence: EvidenceList = Field(default_factory=list)     # source card to show while speaking
    marks: Marks = Field(default_factory=dict)


# ----------------------------------------------------------------------------- additions
class SpeakerInfo(_Model):
    name: str = ""
    side: str = ""                 # "prop" | "opp"; "" = unknown -> first heard prop, then opp


class SessionStart(Event):
    """New debate: carries the motion + speaker sides, triggers pre-research, resets state."""
    event: Literal["SessionStart"] = "SessionStart"
    motion: str = ""
    speakers: dict[Speaker, SpeakerInfo] = Field(default_factory=dict)
    mode: str = "live"             # live | drive | replay | mock


class ResearchProgress(Event):
    event: Literal["ResearchProgress"] = "ResearchProgress"
    stage: str = "searching"       # searching | fetching | embedding | ready | error
    done: int = 0
    total: int = 0
    detail: str = ""


class MapNode(_Model):
    claim_id: str
    speaker: Speaker
    side: Optional[str] = None
    text: str = ""
    type: str = "other"
    importance: float = 0.5
    status: str = "open"           # minor | open | answered | conceded | unanswered
    verdict: Optional[str] = None
    confidence: Optional[float] = None
    turn: int = 0


class MapEdge(_Model):
    src: str                       # claim that links...
    dst: str                       # ...to this earlier claim
    relation: Literal["supports", "attacks"]


class Score(_Model):
    supported: int = 0
    contradicted: int = 0
    missing_context: int = 0
    unverifiable: int = 0
    unanswered: int = 0
    conceded: int = 0
    score: float = 0.5


class ArgMapSnapshot(Event):
    """Full argument map (not a diff): idempotent, survives a browser refresh."""
    event: Literal["ArgMapSnapshot"] = "ArgMapSnapshot"
    version: int = 0
    motion: str = ""
    nodes: list[MapNode] = Field(default_factory=list)
    edges: list[MapEdge] = Field(default_factory=list)
    score: dict[str, Score] = Field(default_factory=dict)   # keyed by side ("prop"/"opp")
    call: str = ""                 # "prop ahead on the evidence (9 checked claims)"
    turns: list[dict] = Field(default_factory=list)          # [{idx, speaker, side, claims[], closed}]
    nonresponsive: list[dict] = Field(default_factory=list)  # [{turn, side, ignored[claim ids], final}]


class DirectorDecision(Event):
    """Every interjection the Director considered, spoken or not (explainability + tuning)."""
    event: Literal["DirectorDecision"] = "DirectorDecision"
    key: str = ""
    kind: str = ""
    claim_id: Optional[str] = None
    action: str = "skipped"        # candidate | held | fired | skipped | dropped
    reason: str = ""               # "cooldown", "low_conf", "speaker_talking", ...
    score: float = 0.0
    target: Optional[str] = None   # speaker the line would address
    text: str = ""                 # what it would say (lets D pre-synthesise TTS / show muted lines)


class DirectorStatus(Event):
    """~1 Hz heartbeat answering "why is it silent?" (D's status chip)."""
    event: Literal["DirectorStatus"] = "DirectorStatus"
    floor_open: bool = False
    tts_busy: bool = False
    cooldown_left_s: float = 0.0
    muted: bool = False
    aggr: int = 3
    pending: list[dict] = Field(default_factory=list)       # top candidates [{key, kind, score}]
    pressure: dict[str, int] = Field(default_factory=dict)  # interjections that put each speaker on the spot
    blocked: str = ""              # why silent: nothing_pending | own_tts | floor_closed | awaiting_pause | muted


class Control(Event):
    """Runtime switches from the dashboard/CLI; logged so replays reproduce them.
    flags: tts, director, web_fallback, interims (bool) | agent_speaking (bool, sent by the browser
    around TTS playback) | mute (bool), force_next, veto (value = decision key), aggr (value = 1..5),
    time (value = speaker id to wrap up)."""
    event: Literal["Control"] = "Control"
    flag: str
    value: Any = True

    @model_validator(mode="after")
    def _typed_value(self):
        """"false"/"0"/"off" on a bool flag is False (bool("false") would be True); aggr "4" -> 4."""
        v = self.value
        if self.flag in BOOL_FLAGS and isinstance(v, (str, int)) and not isinstance(v, bool):
            s = str(v).strip().lower()
            self.value = True if s in _TRUE else False if s in _FALSE else v
        elif self.flag == "aggr" and isinstance(v, (str, float)) and isinstance(_num(v), float):
            self.value = round(_num(v))
        return self


BOOL_FLAGS = {"tts", "director", "web_fallback", "interims", "agent_speaking", "mute"}
_TRUE, _FALSE = {"1", "true", "yes", "on", "t", "y"}, {"0", "false", "no", "off", "f", "n", ""}


class ModuleStatus(Event):
    event: Literal["ModuleStatus"] = "ModuleStatus"
    module: str
    state: str = "up"              # up | degraded | down
    detail: str = ""


class TTSState(Event):
    """Sent by the ONE audio-playing dashboard tab (/?audio=1) when TTS playback starts/ends.
    Consumers: Director (don't talk over itself), A (gate mic frames), brain echo guard.
    Control(flag="agent_speaking", value=bool) is accepted as an equivalent."""
    event: Literal["TTSState"] = "TTSState"
    speaking: bool
    interjection_id: Optional[str] = None


KNOWN = (Utterance, Pause, Claim, Verdict, SourceCard, Interjection, SessionStart, ResearchProgress,
         ArgMapSnapshot, DirectorDecision, DirectorStatus, Control, ModuleStatus, TTSState)
EVENT_TYPES = {m.model_fields["event"].default: m for m in KNOWN}

AnyEvent = Annotated[Union[KNOWN], Field(discriminator="event")]   # type: ignore[valid-type]
_ADAPTER: TypeAdapter = TypeAdapter(AnyEvent)


def parse_event(raw: Union[str, bytes, dict]) -> Event:
    """Parse one event. Known types are validated strictly (errors raise ValidationError);
    unknown `event` names come back as a plain Event with every field preserved."""
    if isinstance(raw, (str, bytes)):
        raw = _json_loads(raw)
    if not isinstance(raw, dict) or not raw.get("event"):
        raise ValueError("an event must be a JSON object with an 'event' field")
    if raw["event"] in EVENT_TYPES:
        return _ADAPTER.validate_python(raw)
    return Event.model_validate(raw)


def _json_loads(raw: Union[str, bytes]) -> Any:
    import json
    return json.loads(raw)


__all__ = [n for n in dir() if not n.startswith("_")] + ["ValidationError"]
