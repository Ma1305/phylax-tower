"""Phylax Tower v2 data contracts: one Pydantic model per collection in docs/v2/CONTRACTS.md.

Rules:
- Callsigns are uppercase with no spaces (AC8646, TRUCK1); stations TOWER / GROUND / OPS.
- Timestamps are timezone-aware UTC datetimes (wall-clock), stored as BSON dates.
- Positions are GeoJSON Points ``{"type": "Point", "coordinates": [lon, lat]}``.
- Agents never act: they insert intents. An intent action is never "clear".

The $jsonSchema validators in scripts/setup_atlas.py read the enums below with
``typing.get_args`` so the models and the database never drift apart.

Write a model to MongoDB with ``to_mongo(model)`` (uses the "from" alias, keeps
datetimes and ObjectIds as BSON types, and drops ``_id`` when it is None).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from bson import ObjectId
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

# --------------------------------------------------------------------------- enums

LaneKind = Literal["runway", "taxiway", "apron"]
LaneState = Literal["clear", "reserved", "occupied", "conflict", "closed"]
ObjectKind = Literal["aircraft", "vehicle", "person", "unknown"]
ObjectStatus = Literal["airborne", "ground"]
StreamKind = Literal["tower", "ground", "ops", "approach", "other"]
ControllerRole = Literal["local", "ground", "supervisor"]
TransmissionKind = Literal["clearance", "request", "readback", "report", "stop", "other"]
Instruction = Literal[
    "land", "takeoff", "cross", "hold_short", "line_up_wait", "taxi", "go_around", "none"
]
Readback = Literal["matched", "mismatch", "missing", "not_applicable"]
AgentName = Literal["listener", "safety", "procedures", "weather"]
PermissionAgent = Literal["listener", "safety", "procedures", "weather", "system"]
AgentMode = Literal["reactive", "proactive"]
PermissionTier = Literal["autonomous", "needs_approval", "never"]
IntentAction = Literal[
    "warn", "stop", "propose_instruction", "close_lane", "reopen_lane", "set_rule_set"
]
"""What an agent asks the gate to do. Never "clear": the AI cannot clear traffic."""
Severity = Literal["info", "caution", "warning", "critical"]
DecisionOutcome = Literal["executed", "rejected", "pending_approval", "revoked"]
ClipStatus = Literal["queued", "processing", "done", "failed"]
Direction = Literal["in", "out"]

ContextKind = Literal["clearance", "request", "report", "alert", "weather", "note"]
StateEventKind = Literal["lane_status", "permission", "controller", "rule_set", "object_removed"]
SecurityEventKind = Literal["validator_rejected", "read_denied", "write_denied", "not_lane_owner"]
SimRunKind = Literal["normal", "incident"]

AGENTS_WITH_INTENTS: tuple[str, ...] = ("safety", "procedures", "weather")
"""Agents that have their own intents_<agent> collection."""

CONFIDENCE_VERIFIED_MIN = 0.8
"""A transmission with confidence below this can never be marked verified."""

OBJECT_TTL_S = 120
"""objects.expires_at = updated_at + OBJECT_TTL_S."""

TOWER_ID = "LGA"
DB_NAME = "holdshort_v2"
STATIONS: tuple[str, ...] = ("TOWER", "GROUND", "OPS")
UNKNOWN_SPEAKER = "UNKNOWN"
HOLDSHORT = "HOLDSHORT"

# --------------------------------------------------------------------- primitives

CALLSIGN_PATTERN = r"^[A-Z0-9]+$"
RUNWAY_PATTERN = r"^[0-3][0-9]$"

Callsign = Annotated[str, StringConstraints(pattern=CALLSIGN_PATTERN)]
Runway = Annotated[str, StringConstraints(pattern=RUNWAY_PATTERN)]
UtcDatetime = AwareDatetime


def _to_object_id(v: Any) -> Any:
    if isinstance(v, str) and ObjectId.is_valid(v):
        return ObjectId(v)
    return v


DocId = Annotated[
    ObjectId,
    BeforeValidator(_to_object_id),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string"}),
]
"""A MongoDB ObjectId (accepts its 24-hex string form; serialises to str in JSON)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, arbitrary_types_allowed=True)


class Point(_Model):
    type: Literal["Point"] = "Point"
    coordinates: list[float] = Field(min_length=2, max_length=3, description="[lon, lat]")


class LineString(_Model):
    type: Literal["LineString"] = "LineString"
    coordinates: list[list[float]]


class Polygon(_Model):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]]


# ------------------------------------------------------------------------- world


class Tower(_Model):
    id: str = Field(alias="_id")
    name: str
    icao: str
    center: Point


class Lane(_Model):
    id: str = Field(alias="_id")
    tower_id: str
    kind: LaneKind
    name: str
    designators: list[str] = Field(default_factory=list)
    width_m: float | None = None
    centerline: LineString | None = None
    area: Polygon
    crosses: list[str] = Field(default_factory=list)


class HoldPoint(_Model):
    id: str = Field(alias="_id")
    tower_id: str
    lane_id: str
    protects: str
    position: Point


class Controller(_Model):
    id: str = Field(alias="_id")
    tower_id: str
    name: str
    role: ControllerRole
    lanes: list[str] = Field(default_factory=list)
    streams: list[str] = Field(default_factory=list)
    active: bool = True


class Stream(_Model):
    id: str = Field(alias="_id")
    tower_id: str
    name: str
    kind: StreamKind
    frequency_mhz: float | None = None
    controller_id: str
    station: str


# -------------------------------------------------------------------- live state


class Clearance(_Model):
    instruction: Instruction
    lane_id: str | None = None
    at: str | None = None
    since: UtcDatetime
    transmission_id: DocId | None = None


class TrackedObject(_Model):
    """collection: objects (TTL on expires_at)."""

    id: str = Field(alias="_id")
    tower_id: str
    kind: ObjectKind
    callsign: Callsign | None = None
    label: str | None = None
    status: ObjectStatus
    position: Point
    alt_ft: float | None = None
    heading_deg: float | None = None
    speed_kt: float | None = None
    lane_id: str | None = None
    clearance: Clearance | None = None
    hold_short: bool = False
    updated_at: UtcDatetime
    expires_at: UtcDatetime


class Reservation(_Model):
    object_id: str | None = None
    callsign: str
    instruction: Instruction
    since: UtcDatetime
    transmission_id: DocId | None = None


class LaneStatus(_Model):
    id: str = Field(alias="_id")
    tower_id: str
    controller_id: str
    state: LaneState
    occupants: list[str] = Field(default_factory=list)
    reservations: list[Reservation] = Field(default_factory=list)
    reason: str | None = None
    updated_at: UtcDatetime


class SourceRef(_Model):
    coll: str
    id: Any


class ContextItem(_Model):
    """collection: context (TTL on expires_at)."""

    id: DocId | None = Field(default=None, alias="_id")
    tower_id: str
    kind: ContextKind
    summary: str
    callsigns: list[str] = Field(default_factory=list)
    lane_ids: list[str] = Field(default_factory=list)
    stream_id: str | None = None
    instruction: Instruction | None = None
    readback: Readback | None = None
    flag: str | None = None
    source: SourceRef
    t: UtcDatetime
    superseded_by: DocId | None = None
    expires_at: UtcDatetime


class Permission(_Model):
    id: PermissionAgent = Field(alias="_id")
    agent: PermissionAgent
    enabled: bool
    mode: AgentMode
    tier: PermissionTier
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    by: str
    changed_at: UtcDatetime


class Rule(_Model):
    id: str
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class RuleSet(_Model):
    version: str
    name: str
    active: bool
    rules: list[Rule]


class AgentState(_Model):
    id: str = Field(alias="_id")
    resume_token: dict[str, Any] | None = None
    updated_at: UtcDatetime


# -------------------------------------------------------------------------- logs


class PositionMeta(_Model):
    object_id: str
    tower_id: str
    kind: ObjectKind
    callsign: str | None = None


class PositionSample(_Model):
    """collection: positions (time series)."""

    t: UtcDatetime
    meta: PositionMeta
    position: Point
    alt_ft: float | None = None
    heading_deg: float | None = None
    speed_kt: float | None = None
    status: ObjectStatus
    lane_id: str | None = None


class StateEvent(_Model):
    id: DocId | None = Field(default=None, alias="_id")
    t: UtcDatetime
    tower_id: str
    kind: StateEventKind
    ref: str
    doc: dict[str, Any] | None = None
    by: str | None = None


class AudioClip(_Model):
    id: DocId | None = Field(default=None, alias="_id")
    tower_id: str
    stream_id: str
    direction: Direction
    t_start: UtcDatetime
    t_end: UtcDatetime
    duration_s: float = Field(ge=0)
    audio_id: DocId | None = None
    mime: str | None = None
    truth_text: str | None = None
    speaker: str | None = None
    status: ClipStatus
    transmission_id: DocId | None = None
    error: str | None = None
    created_at: UtcDatetime


class Transmission(_Model):
    id: DocId | None = Field(default=None, alias="_id")
    tower_id: str
    stream_id: str
    controller_id: str
    t: UtcDatetime
    t_end: UtcDatetime | None = None
    direction: Direction
    from_: Callsign = Field(alias="from")
    to: Callsign
    kind: TransmissionKind
    instruction: Instruction
    runway: Runway | None = None
    at: str | None = None
    lane_id: str | None = None
    transcript: str
    confidence: float = Field(ge=0.0, le=1.0)
    readback: Readback
    verified: bool = False
    flag: str | None = None
    clip_id: DocId | None = None
    audio_id: DocId | None = None
    synthetic: bool = False
    decision_id: DocId | None = None

    @model_validator(mode="after")
    def _low_confidence_not_verified(self) -> Transmission:
        if self.verified and self.confidence < CONFIDENCE_VERIFIED_MIN:
            raise ValueError(f"verified must be False when confidence < {CONFIDENCE_VERIFIED_MIN}")
        return self


class Intent(_Model):
    """collections: intents_safety / intents_procedures / intents_weather."""

    id: DocId | None = Field(default=None, alias="_id")
    t: UtcDatetime
    tower_id: str
    agent: AgentName = Field(description="Must equal the agent of the collection it is in.")
    action: IntentAction
    severity: Severity
    target: list[Callsign] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)
    lane_id: str | None = None
    runway: Runway | None = None
    stream_id: str | None = None
    reason: str
    conflicts_with: str | None = None
    proposal_text: str | None = None
    detail: str | None = None


class AuthorizedBy(_Model):
    permission_id: str
    rule_set_version: str | None = None


class Decision(_Model):
    """collection: decisions (insert-only)."""

    id: DocId | None = Field(default=None, alias="_id")
    t: UtcDatetime
    tower_id: str
    intent_id: DocId
    agent: AgentName
    intent_action: IntentAction
    severity: Severity
    outcome: DecisionOutcome
    rule: str
    lane_id: str | None = None
    controller_id: str
    stream_id: str | None = None
    text: str | None = None
    target: list[str] = Field(default_factory=list)
    mode: AgentMode
    authorized_by: AuthorizedBy
    approved_by: str | None = None
    revokes: DocId | None = None
    notified: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _executed_proposal_needs_approval(self) -> Decision:
        if (
            self.intent_action == "propose_instruction"
            and self.outcome == "executed"
            and not self.approved_by
        ):
            raise ValueError("an executed propose_instruction requires approved_by")
        return self


class SecurityEvent(_Model):
    """collection: security_events (insert-only). One of agent / controller_id is set."""

    id: DocId | None = Field(default=None, alias="_id")
    t: UtcDatetime
    agent: str | None = None
    controller_id: str | None = None
    kind: SecurityEventKind
    attempted: str
    error_code: int | None = None
    error: str | None = None


class Weather(_Model):
    id: DocId | None = Field(default=None, alias="_id")
    t: UtcDatetime
    tower_id: str
    visibility_sm: float = Field(ge=0)
    wind: str | None = None
    raw: str | None = None


class SimRun(_Model):
    id: DocId | None = Field(default=None, alias="_id")
    tower_id: str
    scenario: str
    kind: SimRunKind
    note: str | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None


def to_mongo(model: BaseModel) -> dict[str, Any]:
    """Dump a model to a dict ready for insert (aliases applied, BSON types kept).

    A None ``_id`` is dropped so MongoDB assigns an ObjectId.
    """
    doc = model.model_dump(by_alias=True, mode="python")
    if "_id" in doc and doc["_id"] is None:
        del doc["_id"]
    return doc
