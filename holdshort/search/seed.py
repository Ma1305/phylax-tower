"""Seed data for the callsigns and incidents collections.

Incident summaries are written in our own words from the public record. They
name no airlines, crews or victims and carry no imagery; `source` points at the
public investigation or report so a reader can check the facts.
"""

from __future__ import annotations

# Callsigns heard on the LGA tower / ground frequencies in the demo, plus a few
# deliberately confusable ones (AC8646 / AC8464 / AC6846, TRUCK1 / TRUCK2).
CALLSIGNS: list[dict] = [
    {
        "_id": "AC8646",
        "spoken": "air canada eight six four six",
        "kind": "aircraft",
        "frequency": "tower",
    },
    {
        "_id": "AC8464",
        "spoken": "air canada eight four six four",
        "kind": "aircraft",
        "frequency": "tower",
    },
    {
        "_id": "AC6846",
        "spoken": "air canada six eight four six",
        "kind": "aircraft",
        "frequency": "tower",
    },
    {"_id": "DL2214", "spoken": "delta two two one four", "kind": "aircraft", "frequency": "tower"},
    {
        "_id": "UA1532",
        "spoken": "united one five three two",
        "kind": "aircraft",
        "frequency": "ground",
    },
    {"_id": "TRUCK1", "spoken": "truck one", "kind": "vehicle", "frequency": "ground"},
    {"_id": "TRUCK2", "spoken": "truck two", "kind": "vehicle", "frequency": "ground"},
    {"_id": "TRUCK3", "spoken": "truck three", "kind": "vehicle", "frequency": "ground"},
    {"_id": "OPS1", "spoken": "ops one", "kind": "vehicle", "frequency": "ground"},
    {"_id": "TOWER", "spoken": "laguardia tower", "kind": "station", "frequency": "tower"},
    {"_id": "GROUND", "spoken": "laguardia ground", "kind": "station", "frequency": "ground"},
]

INCIDENTS: list[dict] = [
    {
        "_id": "lga-2026-03-22",
        "title": "LaGuardia runway 4: landing jet and fire truck",
        "airport": "LGA",
        "date": "2026-03-22",
        "summary": (
            "Late at night a regional jet was cleared to land on runway 4. Around the same "
            "time an airport fire truck responding to another call asked to cross runway 4 at "
            "taxiway Delta and was cleared to cross. The tower called stop as the jet touched "
            "down, but the truck was already on the runway and the landing aircraft struck the "
            "emergency vehicle. A landing clearance and a crossing clearance for the same "
            "runway were active at once."
        ),
        "tags": ["landing", "vehicle crossing", "fire truck", "conflicting clearances"],
        "source": "NTSB preliminary report (2026)",
    },
    {
        "_id": "yyz-2025-09-02",
        "title": "Toronto Pearson: business jet nose onto runway during takeoff",
        "airport": "YYZ",
        "date": "2025-09-02",
        "summary": (
            "A business jet being moved for a maintenance ground test, with engineers on board, "
            "crept past the hold line and its nose ended up a few metres onto an active runway "
            "while an airliner was on its takeoff roll. The departing aircraft passed close by. "
            "The investigation looks at hold-short discipline and the closely spaced runway "
            "layout at Pearson."
        ),
        "tags": ["takeoff", "hold short line", "maintenance taxi", "near miss"],
        "source": "TSB investigation A25O0130",
    },
    {
        "_id": "hnd-2024-01-02",
        "title": "Tokyo Haneda: coast guard aircraft on the runway during landing",
        "airport": "HND",
        "date": "2024-01-02",
        "summary": (
            "A coast guard turboprop was told to taxi to the holding point of runway 34R but "
            "understood it was allowed to enter, and lined up on the runway. A widebody airliner "
            "cleared to land on the same runway collided with it in the dark. The case is a "
            "textbook readback and hold-short misunderstanding."
        ),
        "tags": ["landing", "aircraft on runway", "misheard instruction", "readback"],
        "source": "JTSB investigation report",
    },
    {
        "_id": "aus-2023-02-04",
        "title": "Austin: takeoff clearance with a jet landing on the same runway",
        "airport": "AUS",
        "date": "2023-02-04",
        "summary": (
            "In fog, a controller cleared an airliner for takeoff while a cargo jet was on final "
            "for the same runway, expecting the departure to roll quickly. The departure was "
            "slow to go, the arriving crew could not see it, and they went around just above "
            "it with very little separation."
        ),
        "tags": ["takeoff", "landing", "low visibility", "go around", "same runway"],
        "source": "NTSB final report",
    },
    {
        "_id": "jfk-2023-01-13",
        "title": "JFK: airliner taxis across a runway in use for takeoff",
        "airport": "JFK",
        "date": "2023-01-13",
        "summary": (
            "A widebody airliner taxiing for departure was told to cross runway 4L on the way to "
            "31L but went straight across 4L without the crossing clearance while another "
            "airliner was accelerating for takeoff on 4L. The controller cancelled the takeoff "
            "and the departing jet stopped well short."
        ),
        "tags": ["takeoff", "aircraft crossing", "rejected takeoff", "taxi route"],
        "source": "NTSB final report",
    },
    {
        "_id": "lax-1991-02-01",
        "title": "Los Angeles: landing jet onto a commuter plane waiting on the runway",
        "airport": "LAX",
        "date": "1991-02-01",
        "summary": (
            "At night a controller told a commuter turboprop to line up and wait on runway 24L, "
            "then lost track of it and cleared a jet to land on the same runway. The landing jet "
            "hit the waiting aircraft. The case led to better tower tools for tracking who is on "
            "the runway."
        ),
        "tags": ["landing", "line up and wait", "aircraft on runway", "controller workload"],
        "source": "NTSB AAR-91/08",
    },
    {
        "_id": "lin-2001-10-08",
        "title": "Milan Linate: wrong taxiway in fog onto the takeoff runway",
        "airport": "LIN",
        "date": "2001-10-08",
        "summary": (
            "In thick fog a business jet took the wrong taxiway, misread unclear markings and "
            "entered the main runway while an airliner was taking off. There was no ground "
            "radar in use. The accident pushed Europe toward ground surveillance and better "
            "signage."
        ),
        "tags": ["takeoff", "low visibility", "wrong taxiway", "no ground radar"],
        "source": "ANSV final report",
    },
    {
        "_id": "tfn-1977-03-27",
        "title": "Tenerife: takeoff started without clearance in fog",
        "airport": "TFN",
        "date": "1977-03-27",
        "summary": (
            "On a fog-covered runway, one jumbo jet began its takeoff roll believing it was "
            "cleared while another was still backtracking on the same runway. Overlapping radio "
            "calls blocked a key transmission and the phrase 'OK' was taken as a clearance. It "
            "led to standard phraseology such as using 'takeoff' only in an actual clearance."
        ),
        "tags": ["takeoff", "phraseology", "blocked transmission", "low visibility"],
        "source": "Spanish CIAIAC report",
    },
    {
        "_id": "sfo-2017-07-07",
        "title": "San Francisco: arrival lined up with an occupied taxiway",
        "airport": "SFO",
        "date": "2017-07-07",
        "summary": (
            "At night with one parallel runway closed, an arriving airliner lined up on taxiway "
            "C, where four airliners were waiting to depart, instead of runway 28R. It went "
            "around very low over the waiting aircraft."
        ),
        "tags": ["landing", "taxiway", "go around", "runway closure"],
        "source": "NTSB AIR-18/01",
    },
    {
        "_id": "bos-2023-02-27",
        "title": "Boston Logan: business jet takes off without clearance across a landing",
        "airport": "BOS",
        "date": "2023-02-27",
        "summary": (
            "A business jet was told to line up and wait on runway 9 and read that back, then "
            "started its takeoff roll anyway while an airliner was landing on the intersecting "
            "runway 4R. The surface alerting system warned the tower, the controller sent the "
            "arrival around and it climbed away just above the departing jet."
        ),
        "tags": ["takeoff", "landing", "line up and wait", "intersecting runways", "go around"],
        "source": "NTSB final report",
    },
]
