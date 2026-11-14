"""Action gate: the only component that turns agent intents into decisions.

Agents never act. They insert intents into ``intents_<agent>``; the gate watches those
collections with one change stream, checks the master "system" switch, the agent's
permission (enabled, mode, tier) and the active rule set, routes the decision to the
owner of its lane, and INSERTS one decision per intent. Decisions are never updated or
deleted: approvals, rejections and revocations are new decisions.

    uv run python -m holdshort.gate [--db NAME]                    # run the gate
    uv run python -m holdshort.gate approve <decision_id> <controller_id>
    uv run python -m holdshort.gate reject <decision_id> <controller_id> [reason]
    uv run python -m holdshort.gate revoke <decision_id> <controller_id>
    uv run python -m holdshort.gate set-permission <agent> <by> [--enabled on|off]
                                                   [--mode M] [--tier T]
"""

from holdshort.gate.core import (
    ERROR_STATUS,
    GATE_STATE_ID,
    INTENT_COLLECTION_RE,
    Gate,
    GateError,
    activate_rule_set,
    approve,
    close_lane,
    decide,
    reject,
    reopen_lane,
    revoke,
    set_permission,
)

__all__ = [
    "ERROR_STATUS",
    "GATE_STATE_ID",
    "INTENT_COLLECTION_RE",
    "Gate",
    "GateError",
    "activate_rule_set",
    "approve",
    "close_lane",
    "decide",
    "reject",
    "reopen_lane",
    "revoke",
    "set_permission",
]
