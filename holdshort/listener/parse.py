"""parse(text, context) -> Transmission, using an Anthropic LLM with structured JSON output.

The LLM only labels the call; every value it returns is normalised deterministically
(holdshort.listener.normalize) and the safety-relevant rules are applied here in code,
never left to the model:

- confidence < 0.8, or the LLM says the call is unintelligible / fails -> kind "other",
  instruction "none", verified False, transcript kept, flag "unclear - say again".
- a "stop" with no addressee -> kind "stop", to "UNKNOWN", flagged.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, get_args

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

DEFAULT_MODEL = "claude-opus-5-5"

LlmFn = Callable[[str, dict[str, Any]], dict[str, Any] | None]
"""(transcript, context) -> raw label dict (see LABEL_SCHEMA), or None if unparseable.

parse() uses the Anthropic LLM when LLM_API_KEY is set, else the offline labeller in
holdshort.listener.rules (same output shape)."""

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

Stations (contract callsigns): TOWER (LaGuardia tower), AC8646 (Air Canada 8646, \
"the jet"), TRUCK1 (Truck 1 and company, the fire trucks). Write callsigns as \
uppercase with no spaces; use UNKNOWN when no station is named.

kind:
- clearance: the tower issues an instruction (cleared to land / for takeoff, cross \
runway X, hold short, line up and wait, taxi, go around).
- request: a station asks for something. An aircraft's first call on final naming \
a runway is a request to land (instruction land).
- readback: a station repeats a clearance it was given; label the instruction, \
runway and taxiway exactly as read back.
- stop: the tower says "stop".
- other: anything else - position reports after a landing clearance was already \
issued, "stand by", acknowledgements, garbled calls. Use instruction none, runway \
null and at null for other.
instruction: land|takeoff|cross|hold_short|line_up_wait|taxi|go_around|none.
runway: the runway number as spoken digits (e.g. "4"), or null. Wind direction is \
not a runway.
at: the taxiway/intersection (e.g. "Delta"), or null.
intelligible: false if the call is broken up, trails off, asks to "say again", or \
its meaning is unclear. A clear but short call ("stand by") is intelligible.
"""


def _model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL


def _history_lines(context: dict[str, Any]) -> str:
    rows = context.get("history") or []
    out = []
    for h in rows[-8:]:
        out.append(
            f"- {h.get('from')} -> {h.get('to')}: {h.get('kind')} {h.get('instruction')} "
            f"rwy={h.get('runway')} at={h.get('at')} \"{h.get('transcript')}\""
        )
    return "\n".join(out) or "(none)"


def anthropic_llm(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Label one transcript with the Anthropic API (structured JSON output)."""
    import anthropic

    from holdshort.env import redact, require

    key = require("LLM_API_KEY", "Put an Anthropic API key in .env as LLM_API_KEY=...")
    client = anthropic.Anthropic(api_key=key)
    speaker = context.get("speaker")
    user = (
        f"Recent calls on frequency:\n{_history_lines(context)}\n\n"
        + (f"Radio identifies the transmitting station as: {speaker}\n" if speaker else "")
        + f"Call to label:\n\"{text}\""
    )
    try:
        msg = client.beta.messages.create(
            model=_model(),
            max_tokens=4096,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": LABEL_SCHEMA},
            },
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        print(f"listener: LLM call failed: {redact(e)}")
        return None
    if msg.stop_reason in ("refusal", "max_tokens"):
        print(f"listener: LLM stop_reason={msg.stop_reason}; treating call as unparseable")
        return None
    body = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


_NOTICE_SHOWN = False


def get_labeller(name: str = "auto") -> LlmFn:
    """"llm" (Anthropic), "rules" (offline phraseology), or "auto" (llm if LLM_API_KEY set)."""
    global _NOTICE_SHOWN
    from holdshort import env  # noqa: F401  (loads .env)
    from holdshort.listener import rules

    if name == "llm":
        return anthropic_llm
    if name == "rules":
        return rules.label
    if os.environ.get("LLM_API_KEY", "").strip():
        return anthropic_llm
    if not _NOTICE_SHOWN:
        print("listener: LLM_API_KEY not set; using the offline phraseology labeller")
        _NOTICE_SHOWN = True
    return rules.label


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
    transmissions as dicts), audio_ref.
    """
    context = context or {}
    t = context.get("t") or datetime.now(UTC)
    aliases = context.get("aliases")
    confidence = max(0.0, min(1.0, float(confidence)))
    speaker = context.get("speaker")
    base = {
        "t": t,
        "transcript": text,
        "confidence": confidence,
        "readback": "not_applicable",
        "audio_ref": context.get("audio_ref"),
    }

    label = None
    if text.strip():
        try:
            label = (llm or get_labeller())(text, context)
        except Exception as e:  # noqa: BLE001 - a broken label must never crash the listener
            print(f"listener: labelling failed: {type(e).__name__}")
            label = None

    sender = normalize_callsign(speaker or (label or {}).get("from"), aliases)

    unclear = (
        label is None
        or confidence < CONFIDENCE_VERIFIED_MIN
        or not label.get("intelligible", False)
        or label.get("kind") not in get_args(TransmissionKind)
        or label.get("instruction") not in get_args(Instruction)
    )
    if unclear:
        to = normalize_callsign((label or {}).get("to"), aliases)
        return Transmission.model_validate({
            **base, "from": sender, "to": to, "kind": "other", "instruction": "none",
            "runway": None, "at": None, "verified": False, "flag": FLAG_UNCLEAR,
        })

    kind = label["kind"]
    instruction = label["instruction"]
    runway = normalize_runway(label.get("runway"))
    at = normalize_taxiway(label.get("at"))
    to = normalize_callsign(label.get("to"), aliases)
    flag = None
    if kind == "stop":
        instruction, runway, at = "none", None, None
        if to != UNKNOWN and not _mentioned(to, text, aliases):
            to = UNKNOWN  # the LLM inferred an addressee nobody said: don't guess
        if to == UNKNOWN:
            flag = FLAG_STOP_NO_CALLSIGN
    elif kind == "other":
        instruction, runway, at = "none", None, None
    return Transmission.model_validate({
        **base, "from": sender, "to": to, "kind": kind, "instruction": instruction,
        "runway": runway, "at": at, "verified": True, "flag": flag,
    })
