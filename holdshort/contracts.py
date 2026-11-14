"""Hold Short data contracts: the single source of truth for every collection.

Rules (from the build plan):
- Callsigns are uppercase with no spaces (AC8646, TRUCK1, TOWER).
- Runways are two-digit strings ("04", "13", "22"); taxiways are letters ("D").
- Timestamps are timezone-aware UTC datetimes, stored as BSON dates.
- Agents never act: they insert intents. An intent action is never "clear".

The $jsonSchema validators in scripts/setup_atlas.py read the enums below with
``typing.get_args`` so the models and the database never drift apart. If you add
a field here, add it to the validator too.

Write a model to MongoDB with ``to_mongo(model)`` (uses the "from" alias, keeps
datetimes and ObjectIds as BSON types).
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

TransmissionKind = Literal["clearance", "request", "readback", "stop", "other"]
"""What a radio call is."""

Instruction = Literal[
    "land", "takeoff", "cross", "hold_short", "line_up_wait", "taxi", "go_around", "none"
]
"""The instruction carried by a radio call ("none" when there is no instruction)."""

Readback = Literal["matched", "mismatch", "missing", "not_applicable"]
"""Whether the receiving station read the instruction back correctly."""

AgentName = Literal["listener", "weather", "safety", "procedures"]
"""Agents that write intents. Agent identity is the collection it writes to."""

PermissionAgent = Literal["listener", "weather", "safety", "procedures", "system"]
"""Permission switches: one per agent plus the "system" master switch."""

IntentAction = Literal[
    "stop", "propose_instruction", "close_runway", "reopen_runway", "set_rule_set"
]
"""What an agent asks the action gate to do. Never "clear": the AI cannot clear traffic.

``set_rule_set`` (weather agent): proposal_text holds the rule set version to activate.
"""

DecisionOutcome = Literal["executed", "rejected", "pending_approval", "revoked"]
"""What the action gate decided for an intent."""

PermissionTier = Literal["autonomous", "needs_approval", "never"]
"""How much an agent may do without a controller."""

AGENTS_WITH_INTENTS: tuple[str, ...] = ("safety", "procedures", "weather")
"""Agents that have their own intents_<agent> collection."""

CONFIDENCE_VERIFIED_MIN = 0.8
"""A transmission with confidence below this can never be marked verified."""

# --------------------------------------------------------------------- primitives

CALLSIGN_PATTERN = r"^[A-Z0-9]+$"
RUNWAY_PATTERN = r"^[0-3][0-9]$"

Callsign = Annotated[str, StringConstraints(pattern=CALLSIGN_PATTERN)]
"""Uppercase callsign without spaces, e.g. "AC8646", "TRUCK1", "TOWER"."""

Runway = Annotated[str, StringConstraints(pattern=RUNWAY_PATTERN)]
"""Two-digit runway designator, e.g. "04"."""

UtcDatetime = AwareDatetime
"""Timezone-aware datetime (use UTC)."""


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
    """Base: ignores Mongo's ``_id`` on read, allows ObjectId fields."""

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, arbitrary_types_allowed=True
    )


# ------------------------------------------------------------------------- models


class Transmission(_Model):
    """One radio call, as heard and parsed by the listener (collection: transmissions)."""

    t: UtcDatetime = Field(description="Scenario time of the call (UTC).")
    from_: Callsign = Field(alias="from", description="Station that transmitted.")
    to: Callsign = Field(description="Station addressed.")
    kind: TransmissionKind
    instruction: Instruction
    runway: Runway | None = None
    at: str | None = Field(default=None, description='Taxiway / intersection, e.g. "D".')
    transcript: str = Field(description="Text of the call.")
    confidence: float = Field(ge=0.0, le=1.0, description="Speech/parse confidence, 0..1.")
    readback: Readback
    verified: bool = Field(
        default=False,
        description=f"Trusted for safety checks. Always False if confidence < "
        f"{CONFIDENCE_VERIFIED_MIN}.",
    )
    audio_ref: str | None = Field(default=None, description="Path/URL of the audio clip.")

    @model_validator(mode="after")
    def _low_confidence_not_verified(self) -> Transmission:
        if self.verified and self.confidence < CONFIDENCE_VERIFIED_MIN:
            raise ValueError(
                f"verified must be False when confidence < {CONFIDENCE_VERIFIED_MIN}"
            )
        return self


class Intent(_Model):
    """A request from an agent to the action gate (collections: intents_<agent>)."""

    t: UtcDatetime
    agent: AgentName = Field(description="Must equal the agent of the collection it is in.")
    action: IntentAction
    target: list[Callsign] = Field(default_factory=list, description="Callsigns affected.")
    runway: Runway | None = None
    reason: str = Field(description="Human-readable why, shown in the audit timeline.")
    conflicts_with: str | None = Field(
        default=None, description="What this intent guards against (e.g. a callsign)."
    )
    proposal_text: str | None = Field(
        default=None, description="For propose_instruction: the instruction text proposed."
    )


class AuthorizedBy(_Model):
    """Which permission and rule set allowed a decision."""

    permission_id: DocId
    rule_set_version: str


class Decision(_Model):
    """The action gate's verdict on one intent (collection: decisions, insert-only)."""

    t: UtcDatetime
    intent_id: DocId
    agent: AgentName
    intent_action: IntentAction
    outcome: DecisionOutcome
    rule: str = Field(description='Rule id or reason code, e.g. "agent_disabled".')
    authorized_by: AuthorizedBy
    approved_by: str | None = Field(default=None, description="Controller who approved.")
    revokes: DocId | None = Field(default=None, description="Decision this one revokes.")
    notified: list[str] = Field(default_factory=list, description="Who was told.")

    @model_validator(mode="after")
    def _executed_proposal_needs_approval(self) -> Decision:
        if (
            self.intent_action == "propose_instruction"
            and self.outcome == "executed"
            and not self.approved_by
        ):
            raise ValueError("an executed propose_instruction requires approved_by")
        return self


class Permission(_Model):
    """One agent's switch and tier (collection: permissions, unique on agent)."""

    agent: PermissionAgent
    enabled: bool
    tier: PermissionTier
    by: str = Field(description="Who last changed it.")
    changed_at: UtcDatetime


class Rule(_Model):
    """One rule in a rule set."""

    id: str
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class RuleSet(_Model):
    """A versioned set of rules; exactly one should be active (collection: rules)."""

    version: str
    name: str
    active: bool
    rules: list[Rule]


class Position(_Model):
    """A simple map position (WGS84 degrees)."""

    lat: float
    lon: float


class FlightState(_Model):
    """Latest known state of one callsign (collection: flight_state, TTL on expires_at)."""

    callsign: Callsign
    current_clearance: Instruction | None = None
    runway: Runway | None = None
    hold_short: bool = Field(default=False, description="Told to hold short / stopped.")
    position: Position | None = Field(
        default=None, description="Simulated map position, written by the scenario runner."
    )
    updated_at: UtcDatetime
    expires_at: UtcDatetime = Field(description="TTL: the document is deleted after this time.")


class Metar(_Model):
    """One weather observation emitted by the scenario runner (collection: metar)."""

    t: UtcDatetime
    visibility_sm: float = Field(ge=0, description="Visibility in statute miles.")
    wind: str = Field(description='Wind group, e.g. "03012KT".')


class SecurityEvent(_Model):
    """A blocked write attempt by a misbehaving agent (collection: security_events, insert-only)."""

    t: UtcDatetime = Field(description="Wall-clock time of the attempt (UTC).")
    agent: AgentName
    attempted: str = Field(description='What it tried, e.g. "intents_procedures action=clear".')
    error_code: int | None = Field(
        default=None, description="MongoDB error code (121 = document failed validation)."
    )
    error: str = Field(description="Error message returned by Atlas.")


def to_mongo(model: BaseModel) -> dict[str, Any]:
    """Dump a model to a dict ready for insert (aliases applied, BSON types kept)."""
    return model.model_dump(by_alias=True, mode="python")
