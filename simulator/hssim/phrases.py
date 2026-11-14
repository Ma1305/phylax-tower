"""Radio phraseology: how callsigns, runways and taxiways are spoken.

Everything the simulator says is built here so the wording stays standard and the
tower's listener can parse it.
"""

from __future__ import annotations

import re

DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"]
TELEPHONY = {"AC": "Air Canada", "DL": "Delta", "UA": "United", "TRUCK": "Truck", "OPS": "Ops",
             "JZA": "Jazz", "FFT": "Frontier", "UAL": "United"}
PHONETIC = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot",
    "G": "Golf", "H": "Hotel", "J": "Juliet", "K": "Kilo", "L": "Lima", "M": "Mike",
    "N": "November", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
}
STATION_NAMES = {"TOWER": "LaGuardia tower", "GROUND": "LaGuardia ground", "OPS": "LaGuardia ops"}


def digits(text: str) -> str:
    """'8646' -> 'eight six four six' (nine is spoken 'niner')."""
    return " ".join(DIGITS[int(c)] for c in text if c.isdigit())


def spoken_callsign(callsign: str) -> str:
    """AC8646 -> 'Air Canada eight six four six'; TRUCK1 -> 'Truck one'."""
    m = re.fullmatch(r"([A-Z]+)(\d*)", callsign or "")
    if not m:
        return callsign
    prefix, num = m.groups()
    head = TELEPHONY.get(prefix, prefix.title())
    return f"{head} {digits(num)}".strip()


def spoken_runway(runway: str) -> str:
    """'04' -> 'four', '13' -> 'one three', '22' -> 'two two'."""
    return digits(str(int(runway)))


def spoken_taxiway(letter: str) -> str:
    return PHONETIC.get(letter.upper(), letter)


def station_name(station: str) -> str:
    return STATION_NAMES.get(station, station.title())


def find_callsigns(text: str, known: list[str]) -> list[str]:
    """Which of the ``known`` callsigns are named (in spoken form) in ``text``."""
    low = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    low = re.sub(r"\s+", " ", low)
    found = [(low.find(spoken_callsign(c).lower()), c) for c in known]
    # longest spoken form first, so "Truck one" does not also match inside "Truck one one"
    return [c for pos, c in sorted(found) if pos >= 0]


def estimate_duration_s(text: str) -> float:
    """Roughly how long a controller or pilot takes to say ``text`` on the radio."""
    words = len(text.split())
    return round(max(1.2, 0.5 + words / 3.1), 2)
