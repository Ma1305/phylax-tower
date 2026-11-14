"""Action gate: the only component that turns agent intents into decisions.

Agents never act. They insert intents into ``intents_<agent>``; the gate watches
those collections with one change stream, checks the agent's permission, the
master "system" switch and the active rule set, and INSERTS one decision per
intent. Decisions are never updated or deleted: approvals and revocations are
new decisions.

    uv run python -m holdshort.gate                 # run the gate (Ctrl-C to stop)
    uv run python -m holdshort.gate approve <intent_id> <controller>
    uv run python -m holdshort.gate revoke <decision_id> <controller>
    uv run python -m holdshort.gate set-permission <agent> on|off <by>
"""

from holdshort.gate.core import (
    GATE_STATE_ID,
    INTENT_COLLECTION_RE,
    Gate,
    GateError,
    approve,
    decide,
    revoke,
    set_permission,
)

__all__ = [
    "GATE_STATE_ID",
    "INTENT_COLLECTION_RE",
    "Gate",
    "GateError",
    "approve",
    "decide",
    "revoke",
    "set_permission",
]
