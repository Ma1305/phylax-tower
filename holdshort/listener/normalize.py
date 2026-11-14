"""Deterministic normalisation of spoken callsigns, runways and taxiways.

The LLM is asked for contract-shaped values, but whatever it returns is passed
through these functions so the stored values always follow the contracts
(uppercase callsigns without spaces, two-digit runways, single-letter taxiways).
"""

from __future__ import annotations

import re

from holdshort.contracts import CALLSIGN_PATTERN

UNKNOWN = "UNKNOWN"

_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "tree": "3",
    "four": "4", "fower": "4", "five": "5", "fife": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "niner": "9",
}

PHONETIC = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J",
    "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U",
    "victor": "V", "whiskey": "W", "xray": "X", "x-ray": "X", "yankee": "Y", "zulu": "Z",
}

# Spoken telephony designators -> ICAO-ish prefixes used in our callsigns.
TELEPHONY = {"air canada": "AC", "truck": "TRUCK"}

# Words that are noise around a callsign ("Truck one and company", "LaGuardia tower").
_NOISE = re.compile(r"\b(and company|and co|laguardia|la guardia|the)\b")

DEFAULT_ALIASES: dict[str, str] = {
    "tower": "TOWER",
    "laguardia tower": "TOWER",
    "the jet": "AC8646",
    "jet": "AC8646",
    "the aircraft": "AC8646",
    "the airplane": "AC8646",
    "air canada": "AC8646",
    "the truck": "TRUCK1",
    "the trucks": "TRUCK1",
    "trucks": "TRUCK1",
    "the fire truck": "TRUCK1",
    "fire truck": "TRUCK1",
    "truck": "TRUCK1",
}
"""Scenario aliases: indirect references resolved to the one station they mean."""


def _spoken_digits(text: str) -> str:
    """'eight six four six' -> '8646'; leaves other words alone."""
    words = re.split(r"[\s\-]+", text)
    out: list[str] = []
    for w in words:
        d = _DIGITS.get(w)
        if d is not None and out and out[-1].isdigit():
            out[-1] += d
        elif d is not None:
            out.append(d)
        elif w.isdigit() and out and out[-1].isdigit():
            out[-1] += w
        elif w:
            out.append(w)
    return " ".join(out)


def normalize_callsign(raw: str | None, aliases: dict[str, str] | None = None) -> str:
    """Spoken or written callsign -> contract callsign; UNKNOWN if it can't be resolved.

    >>> normalize_callsign("Truck 1 and company")
    'TRUCK1'
    >>> normalize_callsign("the jet")
    'AC8646'
    """
    if not raw:
        return UNKNOWN
    aliases = {**DEFAULT_ALIASES, **(aliases or {})}
    text = re.sub(r"[^\w\s\-]", " ", raw.lower()).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or text in {"unknown", "none", "null", "all", "all stations"}:
        return UNKNOWN
    if text in aliases:
        return aliases[text]
    text = _spoken_digits(text)
    stripped = re.sub(r"\s+", " ", _NOISE.sub(" ", text)).strip()
    if stripped in aliases:
        return aliases[stripped]
    for spoken, prefix in TELEPHONY.items():
        m = re.fullmatch(rf"{spoken}\s*(\d+)", stripped)
        if m:
            return f"{prefix}{m.group(1)}"
    candidate = re.sub(r"[\s\-]", "", stripped).upper()
    if candidate and re.fullmatch(CALLSIGN_PATTERN, candidate):
        if candidate in {"TOWER", "LAGUARDIATOWER"}:
            return "TOWER"
        return candidate
    return UNKNOWN


def normalize_runway(raw: str | int | None) -> str | None:
    """'4', 'four', 'runway 4', '04L' -> '04'; '22' -> '22'; None if not a runway.

    >>> normalize_runway("4")
    '04'
    """
    if raw is None:
        return None
    text = str(raw).lower().replace("runway", " ")
    text = _spoken_digits(re.sub(r"[^\w\s\-]", " ", text).strip())
    m = re.search(r"\d+", text)
    if not m:
        return None
    n = int(m.group(0))
    if not 1 <= n <= 36:
        return None
    return f"{n:02d}"


def normalize_taxiway(raw: str | None) -> str | None:
    """'Delta', 'at D', 'taxiway delta' -> 'D'; None if empty."""
    if not raw:
        return None
    text = re.sub(r"[^\w\s\-]", " ", str(raw).lower())
    text = re.sub(r"\b(at|taxiway|intersection|the)\b", " ", text).strip()
    if not text or text in {"none", "null"}:
        return None
    parts = text.split()
    letters = [PHONETIC.get(p, p.upper()) for p in parts]
    out = "".join(letters)
    return out if re.fullmatch(r"[A-Z][0-9]?", out) else None
