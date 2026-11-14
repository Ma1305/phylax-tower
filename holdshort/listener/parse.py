"""parse(text, context) -> Transmission, rules first, then a model
(holdshort.llm, structured output) for calls the rules cannot fully parse.

The LLM only labels the call; every value it returns is normalised deterministically
(holdshort.listener.normalize) and the safety-relevant rules are applied here in code,
never left to the model:

- confidence < 0.8, or the LLM says the call is unintelligible / fails -> kind "other",
  instruction "none", verified False, transcript kept, flag "unclear - say again".
- a "stop" with no addressee -> kind "stop", to "UNKNOWN", flagged.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field

from holdshort.contracts import (
    CONFIDENCE_VERIFIED_MIN,
    Instruction,
    Transmission,
    TransmissionKind,
)
from holdshort.listener.normalize import (
    UNKNOWN,
    normalize_callsign,
    normalize_runway,
    normalize_taxiway,
)

FLAG_UNCLEAR = "unclear - say again"
FLAG_STOP_NO_CALLSIGN = "stop - no callsign, applies to all traffic"

log = logging.getLogger("holdshort.listener")

LlmFn = Callable[[str, dict[str, Any]], dict[str, Any] | None]
"""(transcript, context) -> raw label dict (see LABEL_SCHEMA), or None if unparseable.

parse() uses hybrid_label (offline rules first, the holdshort.llm model second) when a
provider is configured, else the offline labeller in holdshort.listener.rules."""

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intelligible": {
            "type": "boolean",
            "description": "False if the call is garbled, incomplete or has no clear meaning.",
        },
        "from": {"type": "string", "description": "Station transmitting, e.g. AC8646, TOWER."},
        "to": {"type": "string", "description": "Station addressed; UNKNOWN if none named."},
        "kind": {"type": "string", "enum": list(get_args(TransmissionKind))},
        "instruction": {"type": "string", "enum": list(get_args(Instruction))},
        "runway": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["intelligible", "from", "to", "kind", "instruction", "runway", "at"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You label tower radio calls at LaGuardia for a safety system. Label only what was \
actually said. Never guess or fill in a missing callsign, runway or taxiway.

Stations (contract callsigns): TOWER (LaGuardia tower), GROUND (LaGuardia ground), \
OPS (airport operations), aircraft such as AC8646 (Air Canada 8646), vehicles such as \
TRUCK1 (Truck 1 and company). Write callsigns as uppercase with no spaces; use UNKNOWN \
when no station is named.

kind:
- clearance: the tower issues an instruction (cleared to land / for takeoff, cross \
runway X, hold short, line up and wait, taxi, go around).
- request: a station asks for something. An aircraft's first call on final naming \
a runway is a request to land (instruction land).
- readback: a station repeats a clearance it was given; label the instruction, \
runway and taxiway exactly as read back.
- stop: the controller says "stop".
- report: a station reports its position or state: holding short, clear of the \
runway, runway vacated, airborne, on final after a landing clearance. Use instruction \
none; keep the runway / taxiway named.
- other: anything else - "stand by", acknowledgements, garbled calls. Use \
instruction none, runway null and at null for other.
instruction: land|takeoff|cross|hold_short|line_up_wait|taxi|go_around|none.
runway: the runway number as spoken digits (e.g. "4"), or null. Wind direction is \
not a runway.
at: the taxiway/intersection (e.g. "Delta"), or null. A request to cross ("get across", \
"cross") runway X at a taxiway is kind request, instruction cross, runway X, at the taxiway.
intelligible: false if the call is broken up, trails off, asks to "say again", or \
its meaning is unclear. A clear but short call ("stand by") is intelligible.
"""


def _history_lines(context: dict[str, Any]) -> str:
    rows = context.get("history") or []
    out = []
    for h in rows[-8:]:
        out.append(
            f"- {h.get('from')} -> {h.get('to')}: {h.get('kind')} {h.get('instruction')} "
            f"rwy={h.get('runway')} at={h.get('at')} \"{h.get('transcript')}\""
        )
    return "\n".join(out) or "(none)"


class ParseLabel(BaseModel):
    """Structured output of the model labeller (same shape as LABEL_SCHEMA)."""

    model_config = ConfigDict(populate_by_name=True)
    intelligible: bool = Field(
        description="False if the call is garbled, incomplete or has no clear meaning.")
    from_: str = Field(alias="from", description="Station transmitting, e.g. AC8646, TOWER.")
    to: str = Field(description="Station addressed; UNKNOWN if none named.")
    kind: TransmissionKind
    instruction: Instruction
    runway: str | None = Field(default=None, description='Runway digits, e.g. "4", or null.')
    at: str | None = Field(default=None, description='Taxiway/intersection, e.g. "Delta".')


def _user_prompt(text: str, context: dict[str, Any]) -> str:
    speaker = context.get("speaker")
    return (
        f"Recent calls on frequency:\n{_history_lines(context)}\n\n"
        + (f"Radio identifies the transmitting station as: {speaker}\n" if speaker else "")
        + f"Call to label:\n\"{text}\""
    )


def llm_label(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Label one transcript with holdshort.llm (task "parse"); None on timeout/failure."""
    from holdshort import llm

    timeout_s = float(os.environ.get("LLM_PARSE_TIMEOUT_S", "").strip() or 6)
    out = llm.structured("parse", SYSTEM_PROMPT, _user_prompt(text, context), ParseLabel,
                         timeout_s)
    return out.model_dump(by_alias=True) if out is not None else None


def rules_complete(label: dict[str, Any] | None) -> bool:
    """True if the offline rules fully parsed the call: known phraseology, intelligible,
    sender resolved and an instruction (or a stop / report) recognised."""
    if not label or not label.get("intelligible"):
        return False
    if label.get("from") in (None, "", UNKNOWN):
        return False
    kind = label.get("kind")
    if kind in ("stop", "report"):
        return True
    return kind in ("clearance", "request", "readback") and label.get("instruction") != "none"


def hybrid_label(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Rules first, model second: the safety path never waits on a model when the
    phraseology is standard. If the model fails or times out, keep the rules result."""
    from holdshort.listener import rules

    rl = rules.label(text, context)
    if rules_complete(rl):
        log.info("listener parse path=rules")
        return rl
    ml = llm_label(text, context)
    if ml is not None:
        log.info("listener parse path=llm")
        return ml
    log.info("listener parse path=rules-fallback")
    return rl


_NOTICE_SHOWN = False


def get_labeller(name: str = "auto") -> LlmFn:
    """"llm" (model only), "rules" (offline phraseology), or "auto" (rules first, then
    the model from holdshort.llm when a provider is configured)."""
    global _NOTICE_SHOWN
    from holdshort import llm
    from holdshort.listener import rules

    if name == "llm":
        return llm_label
    if name == "rules":
        return rules.label
    if not _NOTICE_SHOWN:
        print(f"listener: {llm.describe()}")
        _NOTICE_SHOWN = True
    if llm.provider() != "none":
        return hybrid_label
    return rules.label


STATIONS = frozenset({"TOWER", "GROUND", "OPS"})


def _station(callsign: str, station: str | None) -> str:
    """A controller station heard on a stream is that stream's station."""
    return station if station and callsign in STATIONS else callsign


def _mentioned(callsign: str, text: str, aliases: dict[str, str] | None) -> bool:
    """True if some spoken form of `callsign` occurs in the transcript."""
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    for n in range(1, 7):
        for i in range(len(words) - n + 1):
            if normalize_callsign(" ".join(words[i : i + n]), aliases) == callsign:
                return True
    return False


def parse(
    text: str,
    context: dict[str, Any] | None = None,
    *,
    confidence: float = 1.0,
    llm: LlmFn | None = None,
) -> Transmission:
    """Turn one transcript into a contract Transmission (readback = not_applicable).

    context keys (all optional): t (aware datetime), speaker (callsign hint from
    the radio/script), aliases (extra spoken-name -> callsign map), history (recent
    transmissions as dicts), fields (extra Transmission fields: tower_id, stream_id,
    controller_id, direction, ...), station (the controller station of the stream:
    a controller's call is from it).
    """
    context = context or {}
    t = context.get("t") or datetime.now(UTC)
    aliases = context.get("aliases")
    confidence = max(0.0, min(1.0, float(confidence)))
    speaker = context.get("speaker")
    base = {
        "direction": "in",
        **(context.get("fields") or {}),
        "t": t,
        "transcript": text,
        "confidence": confidence,
        "readback": "not_applicable",
    }
    station = context.get("station")

    label = None
    if text.strip():
        try:
            label = (llm or get_labeller())(text, context)
        except Exception as e:  # noqa: BLE001 - a broken label must never crash the listener
            print(f"listener: labelling failed: {type(e).__name__}")
            label = None

    sender = _station(normalize_callsign(speaker or (label or {}).get("from"), aliases), station)

    unclear = (
        label is None
        or confidence < CONFIDENCE_VERIFIED_MIN
        or not label.get("intelligible", False)
        or label.get("kind") not in get_args(TransmissionKind)
        or label.get("instruction") not in get_args(Instruction)
    )
    if unclear:
        to = _station(normalize_callsign((label or {}).get("to"), aliases), station)
        return Transmission.model_validate({
            **base, "from": sender, "to": to, "kind": "other", "instruction": "none",
            "runway": None, "at": None, "verified": False, "flag": FLAG_UNCLEAR,
        })

    kind = label["kind"]
    instruction = label["instruction"]
    runway = normalize_runway(label.get("runway"))
    at = normalize_taxiway(label.get("at"))
    to = _station(normalize_callsign(label.get("to"), aliases), station)
    if station and sender == UNKNOWN and kind in ("clearance", "stop"):
        sender = station  # only a controller issues clearances and stops
    flag = None
    if kind == "stop":
        instruction, runway, at = "none", None, None
        if to != UNKNOWN and not _mentioned(to, text, aliases):
            to = UNKNOWN  # the LLM inferred an addressee nobody said: don't guess
        if to == UNKNOWN:
            flag = FLAG_STOP_NO_CALLSIGN
    elif kind == "other":
        instruction, runway, at = "none", None, None
    elif kind == "report":
        instruction = "none"
    return Transmission.model_validate({
        **base, "from": sender, "to": to, "kind": kind, "instruction": instruction,
        "runway": runway, "at": at, "verified": True, "flag": flag,
    })
