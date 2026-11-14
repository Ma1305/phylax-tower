"""Offline phraseology labeller: same output shape as the LLM labeller.

Tried first on every call (and used alone with LLM_PROVIDER=none), so the
listener keeps working without network access. It recognises standard tower
phraseology and returns intelligible=False when it cannot make sense of a call,
which parse() turns into a flagged "say again" transmission. It never guesses.
"""

from __future__ import annotations

import re
from typing import Any

from holdshort.listener.normalize import PHONETIC, UNKNOWN, normalize_callsign

_NUM = r"(?:zero|one|two|three|tree|four|fower|five|six|seven|eight|nine|niner|\d+)"
_CALLSIGN_RE = re.compile(
    rf"\b(?:(?:air canada|united|delta)(?: {_NUM})+|(?:truck|ops)(?: {_NUM})(?: and company)?|"
    rf"(?:laguardia )?(?:tower|ground)|ops|the jet)\b"
)
STATIONS = {"TOWER", "GROUND", "OPS"}
_RUNWAY_RE = re.compile(rf"\brunway((?: {_NUM}){{1,2}})(?: (left|right|center))?\b")
_AT_RE = re.compile(rf"\bat ({'|'.join(sorted(PHONETIC, key=len, reverse=True))})\b")

# (pattern, instruction); first match wins.
_INSTRUCTIONS = [
    (r"\bcleared (?:for )?(?:the )?take ?off\b|\btake ?off\b", "takeoff"),
    (r"\bgo around\b", "go_around"),
    (r"\bline up and wait\b", "line_up_wait"),
    (r"\bhold short\b", "hold_short"),
    (r"\bcross(?:ing)? runway\b|\bcross\b", "cross"),
    (r"\bcleared to land\b|\bland(?:ing)?\b", "land"),
    (r"\btaxi\b", "taxi"),
]

_REPORT_RE = re.compile(
    r"\bclear of (?:the )?runway\b|\bvacated\b|\bairborne\b|\bholding short\b|"
    r"\bholding (?:at|on)\b|\bposition\b|\bon final\b|\bwe(?: re| are) clear\b"
)

_UNCLEAR_RE = re.compile(r"\bsay again\b|\.\.\.|\b(?:uh|um)\b.*\b(?:uh|um)\b")


def _norm(text: str) -> str:
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^\w\s.,]", " ", text)  # commas end a callsign
    return re.sub(r"\s+", " ", text).strip()


def label(text: str, context: dict[str, Any]) -> dict[str, Any] | None:
    """Label one transcript like the LLM would (see parse.LABEL_SCHEMA)."""
    raw = text
    text = _norm(text)
    if not text:
        return None
    aliases = context.get("aliases")
    intelligible = not _UNCLEAR_RE.search(raw.lower())
    plain = re.sub(r"\s+", " ", text.replace(".", " ").replace(",", " , ")).strip(" ,")

    spans = [(m.start(), normalize_callsign(m.group(0), aliases))
             for m in _CALLSIGN_RE.finditer(plain)]
    named = [cs for _, cs in spans if cs != UNKNOWN]
    first_at_start = bool(spans) and spans[0][0] == 0
    last_at_end = bool(spans) and plain.rstrip(" ,").endswith(
        _CALLSIGN_RE.findall(plain)[-1]
    )

    speaker = normalize_callsign(context.get("speaker"), aliases) if context.get(
        "speaker") else None
    if speaker and speaker != UNKNOWN:
        sender = speaker
    elif len(named) >= 2 and first_at_start:
        sender = named[1]  # "<addressee>, <sender>, ..."
    elif named and first_at_start and named[0] not in STATIONS:
        sender = "TOWER"  # a controller addressing a station (parse maps it to the stream)
    elif named and last_at_end:
        sender = named[-1]  # readback ends with own callsign
    else:
        sender = UNKNOWN
    others = [cs for cs in named if cs != sender]
    if others and first_at_start:
        to = others[0]
    elif sender not in STATIONS | {UNKNOWN}:
        to = "TOWER"
    else:
        to = UNKNOWN

    m = _RUNWAY_RE.search(plain)
    runway = m.group(1).strip() if m else None
    m = _AT_RE.search(plain)
    at = m.group(1) if m else None
    instruction = next((ins for pat, ins in _INSTRUCTIONS if re.search(pat, plain)), "none")

    if re.search(r"\bstop\b", plain):
        kind = "stop"
        to = others[0] if others and first_at_start else UNKNOWN
    elif re.search(r"\brequest", plain):
        kind = "request"
    elif sender in STATIONS and instruction != "none":
        kind = "clearance"
    elif sender not in STATIONS | {UNKNOWN} and instruction != "none" and last_at_end \
            and not first_at_start:
        kind = "readback"
    elif sender not in STATIONS | {UNKNOWN} and "final" in plain and runway \
            and not _REPORT_RE.search(plain.replace("on final", "")):
        kind, instruction = "request", "land"
    elif sender not in STATIONS | {UNKNOWN} and _REPORT_RE.search(plain):
        kind, instruction = "report", "none"
    else:
        kind = "other"
    if kind in ("other", "stop"):
        instruction, runway, at = "none", None, None
    return {"intelligible": intelligible, "from": sender, "to": to, "kind": kind,
            "instruction": instruction, "runway": runway, "at": at}
