"""Write a small, realistic three-minute history into a scratch database.

    uv run python seed_demo.py --db holdshort_v2_demo

Refuses the real databases (holdshort, holdshort_v2). The collections it writes are emptied
first. Everything is generated: no real recordings, the audio is short tones.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import struct
import sys
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import gridfs
from bson import ObjectId
from pymongo import ASCENDING, GEOSPHERE, MongoClient

import config

TOWER = "LGA"
DURATION_S = 180
WORLD_FILE = Path(__file__).resolve().parents[2] / "docs" / "v2" / "world_lga.json"
COLLECTIONS = [
    "towers",
    "lanes",
    "hold_points",
    "controllers",
    "streams",
    "objects",
    "lane_status",
    "permissions",
    "positions",
    "state_events",
    "audio_clips",
    "transmissions",
    "intents_safety",
    "intents_procedures",
    "intents_weather",
    "decisions",
    "weather",
    "sim_runs",
    "audio.files",
    "audio.chunks",
]
M_PER_DEG_LAT = 111_320.0
ACCESS = {
    "listener": (
        ["audio_clips", "streams", "controllers", "lanes", "objects", "callsigns"],
        ["transmissions", "audio_clips"],
    ),
    "safety": (
        [
            "context",
            "objects",
            "lane_status",
            "lanes",
            "controllers",
            "streams",
            "rules",
            "permissions",
            "weather",
        ],
        ["intents_safety"],
    ),
    "procedures": (
        [
            "context",
            "objects",
            "lane_status",
            "lanes",
            "controllers",
            "streams",
            "rules",
            "decisions",
            "intents_safety",
            "incidents",
        ],
        ["intents_procedures"],
    ),
    "weather": (["weather", "rules"], ["intents_weather"]),
    "system": ([], []),
}
MODES = {
    "listener": ("reactive", "autonomous"),
    "safety": ("proactive", "needs_approval"),
    "procedures": ("proactive", "needs_approval"),
    "weather": ("proactive", "autonomous"),
    "system": ("proactive", "autonomous"),
}


# ---------------------------------------------------------------- geometry


def lane_area(centerline: list[list[float]], width_m: float) -> dict:
    """Polygon around a centerline: each vertex offset half the width to both sides."""
    lat0 = sum(p[1] for p in centerline) / len(centerline)
    kx = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    pts = [(lon * kx, lat * M_PER_DEG_LAT) for lon, lat in centerline]
    half = width_m / 2
    left, right = [], []
    for i, (x, y) in enumerate(pts):
        ax, ay = pts[max(i - 1, 0)]
        bx, by = pts[min(i + 1, len(pts) - 1)]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n * half, dx / n * half
        left.append((x + nx, y + ny))
        right.append((x - nx, y - ny))
    ring = left + right[::-1] + left[:1]
    coords = [[round(x / kx, 7), round(y / M_PER_DEG_LAT, 7)] for x, y in ring]
    return {"type": "Polygon", "coordinates": [coords]}


def inside(point: list[float], polygon: dict) -> bool:
    x, y = point
    ring = polygon["coordinates"][0]
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def lerp(a: list[float], b: list[float], f: float) -> list[float]:
    f = min(max(f, 0.0), 1.0)
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]


def heading(a: list[float], b: list[float]) -> float:
    dx = (b[0] - a[0]) * math.cos(math.radians(a[1]))
    return round(math.degrees(math.atan2(dx, b[1] - a[1])) % 360, 1)


def tone_wav(freq: float, seconds: float, rate: int = 8000) -> bytes:
    """A short two-tone beep with a fade in and out (stands in for a radio call)."""
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / 400, (n - i) / 400)
            f = freq if (i // (rate // 4)) % 2 == 0 else freq * 1.25
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * f * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


# ---------------------------------------------------------------- the story


def movers(world: dict) -> list[dict]:
    """Six objects; each `path(s)` gives (position, alt_ft, speed_kt, status) at second s."""
    pts = world["points"]
    thr04, thr22 = pts["RWY04_THRESHOLD"], pts["RWY22_THRESHOLD"]
    far = lerp(thr04, pts["FINAL_04_10NM"], 8040 / 18520)  # 120 s out at 130 kt
    twy_b = next(x for x in world["lanes"] if x["_id"] == "TWY_B")["centerline"]["coordinates"]
    hp_d = next(h for h in world["hold_points"] if h["_id"] == "HP_D_NW")["position"]["coordinates"]
    apron = next(x for x in world["lanes"] if x["_id"] == "APRON_MAIN")["area"]["coordinates"][0]
    gate_a, gate_b = lerp(apron[0], apron[2], 0.3), lerp(apron[1], apron[3], 0.4)

    def arrival(s):
        if s < 120:
            return lerp(far, thr04, s / 120), round(1400 * (1 - s / 120)), 130, "airborne"
        r = (s - 120) / 60
        return (
            lerp(thr04, thr22, 0.02 + 0.5 * (r - r * r / 2) * 2 * 0.55),
            0,
            round(125 - 95 * r),
            "ground",
        )

    def taxi(s):  # along B to abeam D, then holding
        f = min(s / 110, 1.0)
        return lerp(twy_b[0], lerp(twy_b[0], twy_b[1], 0.42), f), 0, 14 if f < 1 else 0, "ground"

    def truck(s):  # leaves the fire station at 60 s, stops at the D hold point
        f = min(max((s - 60) / 45, 0.0), 1.0)
        return lerp(pts["FIRE_STATION"], hp_d, f), 0, 22 if 0 < f < 1 else 0, "ground"

    def ops(s):  # back and forth on the apron
        f = 0.5 - 0.5 * math.cos(2 * math.pi * s / 120)
        return lerp(gate_a, gate_b, f), 0, 12, "ground"

    def walker(s):
        return lerp(gate_b, lerp(gate_b, apron[2], 0.5), s / 180), 0, 3, "ground"

    def blip(s):  # unidentified low target drifting north of runway 13/31
        a = [pts["RWY13_THRESHOLD"][0] + 0.004, pts["RWY13_THRESHOLD"][1] + 0.003]
        return lerp(a, [a[0] + 0.006, a[1] - 0.001], s / 180), 150, 20, "airborne"

    return [
        {
            "id": "AC8646",
            "kind": "aircraft",
            "callsign": "AC8646",
            "label": "Arrival",
            "path": arrival,
        },
        {
            "id": "JZ7801",
            "kind": "aircraft",
            "callsign": "JZ7801",
            "label": "Taxiing out",
            "path": taxi,
        },
        {
            "id": "TRUCK1",
            "kind": "vehicle",
            "callsign": "TRUCK1",
            "label": "Fire truck",
            "path": truck,
        },
        {"id": "OPS7", "kind": "vehicle", "callsign": "OPS7", "label": "Ops vehicle", "path": ops},
        {
            "id": "OBJ-17",
            "kind": "person",
            "callsign": None,
            "label": "Person on foot",
            "path": walker,
        },
        {
            "id": "OBJ-23",
            "kind": "unknown",
            "callsign": None,
            "label": "Unidentified",
            "path": blip,
        },
    ]


def seed(db, end: datetime | None = None) -> dict:
    if db.name in config.PROTECTED_DBS:
        raise SystemExit(f"refusing to seed {db.name}: that is a real database")
    end = (end or datetime.now(UTC)).replace(microsecond=0)
    t0 = end - timedelta(seconds=DURATION_S)

    def at(s: float) -> datetime:
        return t0 + timedelta(seconds=s)

    for name in COLLECTIONS:
        db.drop_collection(name)
    db.create_collection(
        "positions", timeseries={"timeField": "t", "metaField": "meta", "granularity": "seconds"}
    )
    # the indexes CONTRACTS.md names, so the scratch database behaves like the real one
    db.state_events.create_index(
        [("tower_id", ASCENDING), ("kind", ASCENDING), ("ref", ASCENDING), ("t", ASCENDING)]
    )

    # ---- world
    world = json.loads(WORLD_FILE.read_text())
    lanes = []
    for lane in world["lanes"]:
        lane = {**lane, "tower_id": TOWER}
        if not lane.get("area"):
            lane["area"] = lane_area(lane["centerline"]["coordinates"], lane["width_m"])
        lanes.append(lane)
    controllers = [{**c, "tower_id": TOWER} for c in world["controllers"]]
    streams = [{**s, "tower_id": TOWER} for s in world["streams"]]
    db.towers.insert_one(world["tower"])
    db.lanes.insert_many(lanes)
    db.lanes.create_index([("area", GEOSPHERE)])
    db.hold_points.insert_many([{**h, "tower_id": TOWER} for h in world["hold_points"]])
    db.controllers.insert_many(controllers)
    db.streams.insert_many(streams)
    owner = {lid: c["_id"] for c in controllers for lid in c["lanes"]}
    stream_owner = {s["_id"]: s["controller_id"] for s in streams}

    def lane_of(point, status, alt):
        if status == "airborne" and (alt or 0) >= 200:
            return None
        # runways first, so a crossing point counts as the runway
        for lane in sorted(lanes, key=lambda x: x["kind"] != "runway"):
            if inside(point, lane["area"]):
                return lane["_id"]
        return None

    # ---- positions (1 Hz) and the current objects
    things = movers(world)
    positions, objects = [], []
    for o in things:
        last = None
        for s in range(DURATION_S + 1):
            p, alt, spd, status = o["path"](s)
            nxt = o["path"](min(s + 1, DURATION_S))[0]
            hdg = heading(p, nxt) if nxt != p else (last or {}).get("heading_deg")
            last = {
                "t": at(s),
                "meta": {
                    "object_id": o["id"],
                    "tower_id": TOWER,
                    "kind": o["kind"],
                    "callsign": o["callsign"],
                },
                "position": {"type": "Point", "coordinates": [round(p[0], 6), round(p[1], 6)]},
                "alt_ft": alt,
                "heading_deg": hdg,
                "speed_kt": spd,
                "status": status,
                "lane_id": lane_of(p, status, alt),
            }
            positions.append(last)
        objects.append(
            {
                "_id": o["id"],
                "tower_id": TOWER,
                "kind": o["kind"],
                "callsign": o["callsign"],
                "label": o["label"],
                "status": last["status"],
                "position": last["position"],
                "alt_ft": last["alt_ft"],
                "heading_deg": last["heading_deg"],
                "speed_kt": last["speed_kt"],
                "lane_id": last["lane_id"],
                "clearance": None,
                "hold_short": o["id"] in ("JZ7801", "TRUCK1"),
                "updated_at": end,
                "expires_at": end + timedelta(seconds=120),
            }
        )
    db.positions.insert_many(positions)

    # ---- radio: three streams of clips, each with a generated tone
    bucket = gridfs.GridFSBucket(db, bucket_name="audio")
    tones = {"LGA_TWR": 520.0, "LGA_GND": 660.0, "LGA_OPS": 440.0}
    calls = [  # (s, stream, from, to, kind, instruction, runway, at, lane, text, readback, conf)
        (
            10,
            "LGA_TWR",
            "TOWER",
            "AC8646",
            "clearance",
            "land",
            "04",
            None,
            "RWY_04_22",
            "AC8646, LaGuardia Tower, runway 04, cleared to land.",
            "matched",
            0.96,
        ),
        (
            16,
            "LGA_TWR",
            "AC8646",
            "TOWER",
            "readback",
            "land",
            "04",
            None,
            "RWY_04_22",
            "Cleared to land runway 04, AC8646.",
            "not_applicable",
            0.94,
        ),
        (
            40,
            "LGA_GND",
            "GROUND",
            "JZ7801",
            "clearance",
            "hold_short",
            "04",
            "D",
            "RWY_04_22",
            "JZ7801, taxi via Bravo, hold short runway 04 at Delta.",
            "matched",
            0.93,
        ),
        (
            46,
            "LGA_GND",
            "JZ7801",
            "GROUND",
            "readback",
            "hold_short",
            "04",
            "D",
            "RWY_04_22",
            "Bravo, hold short 04 at Delta, JZ7801.",
            "not_applicable",
            0.91,
        ),
        (
            70,
            "LGA_OPS",
            "OPS",
            "TRUCK1",
            "clearance",
            "hold_short",
            "04",
            "D",
            "RWY_04_22",
            "TRUCK1, Ops, proceed to Delta, hold short runway 04.",
            "matched",
            0.92,
        ),
        (
            76,
            "LGA_OPS",
            "TRUCK1",
            "OPS",
            "readback",
            "hold_short",
            "04",
            "D",
            "RWY_04_22",
            "Proceed to Delta, hold short 04, TRUCK1.",
            "not_applicable",
            0.9,
        ),
        (
            100,
            "LGA_OPS",
            "TRUCK1",
            "OPS",
            "request",
            "cross",
            "04",
            "D",
            "RWY_04_22",
            "Ops, TRUCK1 at Delta, request to cross runway 04.",
            "not_applicable",
            0.88,
        ),
        (
            128,
            "LGA_TWR",
            "AC8646",
            "TOWER",
            "report",
            "none",
            "04",
            None,
            "RWY_04_22",
            "AC8646 on the ground, rolling out.",
            "not_applicable",
            0.74,
        ),
        (
            158,
            "LGA_GND",
            "JZ7801",
            "GROUND",
            "request",
            "cross",
            "04",
            "D",
            "RWY_04_22",
            "Ground, JZ7801 holding short 04 at Delta, ready to cross.",
            "not_applicable",
            0.9,
        ),
    ]
    clips, txs = [], []

    def add_call(
        s,
        stream,
        frm,
        to,
        kind,
        instr,
        rwy,
        at_pt,
        lane,
        text,
        readback,
        conf,
        direction="in",
        decision_id=None,
    ):
        dur = round(1.2 + len(text) / 40, 1)
        clip_id, tx_id = ObjectId(), ObjectId()
        audio_id = bucket.upload_from_stream(
            f"{stream}-{s:03d}.wav",
            tone_wav(tones[stream], min(dur, 3.0)),
            metadata={"mime": "audio/wav", "stream_id": stream},
        )
        clips.append(
            {
                "_id": clip_id,
                "tower_id": TOWER,
                "stream_id": stream,
                "direction": direction,
                "t_start": at(s),
                "t_end": at(s + dur),
                "duration_s": dur,
                "audio_id": audio_id,
                "mime": "audio/wav",
                "truth_text": text,
                "speaker": frm,
                "status": "done",
                "transmission_id": tx_id,
                "error": None,
                "created_at": at(s + dur),
            }
        )
        txs.append(
            {
                "_id": tx_id,
                "tower_id": TOWER,
                "stream_id": stream,
                "controller_id": stream_owner[stream],
                "t": at(s),
                "t_end": at(s + dur),
                "direction": direction,
                "from": frm,
                "to": to,
                "kind": kind,
                "instruction": instr,
                "runway": rwy,
                "at": at_pt,
                "lane_id": lane,
                "transcript": text,
                "confidence": conf,
                "readback": readback,
                "verified": conf >= 0.8,
                "flag": None if conf >= 0.8 else "unclear - say again",
                "clip_id": clip_id,
                "audio_id": audio_id,
                "synthetic": direction == "out",
                "decision_id": decision_id,
            }
        )
        return tx_id

    tx_ids = [add_call(*c) for c in calls]

    # ---- permissions, and the one switch change in the story
    permissions, events = [], []
    for agent, (read, write) in ACCESS.items():
        mode, tier = MODES[agent]
        doc = {
            "_id": agent,
            "agent": agent,
            "enabled": True,
            "mode": mode,
            "tier": tier,
            "read": read,
            "write": write,
            "by": "seed",
            "changed_at": t0,
        }
        permissions.append(doc)
        events.append(
            {
                "_id": ObjectId(),
                "t": t0,
                "tower_id": TOWER,
                "kind": "permission",
                "ref": agent,
                "doc": dict(doc),
                "by": "seed",
            }
        )
    weather_perm = next(p for p in permissions if p["_id"] == "weather")
    weather_perm.update(tier="needs_approval", by="ctl_sup", changed_at=at(150))
    events.append(
        {
            "_id": ObjectId(),
            "t": at(150),
            "tower_id": TOWER,
            "kind": "permission",
            "ref": "weather",
            "doc": dict(weather_perm),
            "by": "ctl_sup",
        }
    )

    # ---- lane status changes, each with its state_events document
    land = {
        "object_id": "AC8646",
        "callsign": "AC8646",
        "instruction": "land",
        "since": at(10),
        "transmission_id": tx_ids[0],
    }
    changes = [  # (s, lane, state, occupants, reservations, reason)
        *[(0, lane["_id"], "clear", [], [], None) for lane in lanes],
        (0, "APRON_MAIN", "occupied", ["OPS7", "OBJ-17"], [], None),
        (2, "TWY_B", "occupied", ["JZ7801"], [], None),
        (11, "RWY_04_22", "reserved", [], [land], None),
        (104, "TWY_D", "occupied", ["TRUCK1"], [], None),
        (121, "RWY_04_22", "occupied", ["AC8646"], [land], None),
    ]
    lane_status: dict[str, dict] = {}
    for s, lane_id, state, occupants, reservations, reason in changes:
        doc = {
            "_id": lane_id,
            "tower_id": TOWER,
            "controller_id": owner.get(lane_id, "ctl_sup"),
            "state": state,
            "occupants": occupants,
            "reservations": reservations,
            "reason": reason,
            "updated_at": at(s),
        }
        lane_status[lane_id] = doc
        events.append(
            {
                "_id": ObjectId(),
                "t": at(s),
                "tower_id": TOWER,
                "kind": "lane_status",
                "ref": lane_id,
                "doc": dict(doc),
                "by": "state",
            }
        )

    # ---- weather and the agents: an advisory, an approved stop, a proposal still pending
    weather = [
        {
            "_id": ObjectId(),
            "t": t0,
            "tower_id": TOWER,
            "visibility_sm": 10,
            "wind": "04008KT",
            "raw": "KLGA 04008KT 10SM FEW050",
        },
        {
            "_id": ObjectId(),
            "t": at(60),
            "tower_id": TOWER,
            "visibility_sm": 2,
            "wind": "05012KT",
            "raw": "KLGA 05012KT 2SM BR OVC008",
        },
    ]

    def intent(s, agent, action, severity, target, lane, stream, reason, text, detail):
        return {
            "_id": ObjectId(),
            "t": at(s),
            "tower_id": TOWER,
            "agent": agent,
            "action": action,
            "severity": severity,
            "target": target,
            "object_ids": target,
            "lane_id": lane,
            "runway": "04" if lane == "RWY_04_22" else None,
            "stream_id": stream,
            "reason": reason,
            "conflicts_with": None,
            "proposal_text": text,
            "detail": detail,
        }

    def decision(s, it, outcome, rule, mode, approved_by=None, revokes=None, tier_perm=None):
        lane = it["lane_id"]
        return {
            "_id": ObjectId(),
            "t": at(s),
            "tower_id": TOWER,
            "intent_id": it["_id"],
            "agent": it["agent"],
            "intent_action": it["action"],
            "severity": it["severity"],
            "outcome": outcome,
            "rule": rule,
            "lane_id": lane,
            "controller_id": owner.get(lane, "ctl_sup"),
            "stream_id": it["stream_id"],
            "text": it["proposal_text"] or it["detail"],
            "target": it["target"],
            "mode": mode,
            "authorized_by": {
                "permission_id": tier_perm or it["agent"],
                "rule_set_version": "normal_v1",
            },
            "approved_by": approved_by,
            "revokes": revokes,
            "notified": [owner.get(lane, "ctl_sup")],
        }

    i_wx = intent(
        62,
        "weather",
        "warn",
        "caution",
        [],
        None,
        None,
        "low_visibility",
        None,
        "Visibility dropped to 2 SM. Consider the low-visibility rule set.",
    )
    i_stop = intent(
        103,
        "safety",
        "stop",
        "critical",
        ["TRUCK1"],
        "RWY_04_22",
        "LGA_OPS",
        "no_crossing_during_active_landing",
        "TRUCK1, hold position. Traffic landing runway 04.",
        "TRUCK1 asked to cross runway 04 at D while AC8646 is cleared to land.",
    )
    i_prop = intent(
        163,
        "procedures",
        "propose_instruction",
        "info",
        ["JZ7801"],
        "RWY_04_22",
        "LGA_GND",
        "sequence_crossing",
        "JZ7801, hold short runway 04, expect crossing after the landing traffic.",
        "JZ7801 is ready at D; AC8646 is still on the runway.",
    )
    i_stop["conflicts_with"] = "AC8646"
    d_wx = decision(63, i_wx, "executed", "advisory", "proactive")
    d_stop = decision(104, i_stop, "pending_approval", "needs_approval", "proactive")
    d_ok = decision(112, i_stop, "executed", "controller_approved", "proactive", "ctl_local")
    d_prop = decision(164, i_prop, "pending_approval", "needs_approval", "proactive")
    add_call(
        114,
        "LGA_OPS",
        "HOLDSHORT",
        "TRUCK1",
        "stop",
        "hold_short",
        "04",
        "D",
        "RWY_04_22",
        i_stop["proposal_text"],
        "not_applicable",
        1.0,
        direction="out",
        decision_id=d_ok["_id"],
    )

    db.audio_clips.insert_many(clips)
    db.transmissions.insert_many(txs)
    db.objects.insert_many(objects)
    db.permissions.insert_many(permissions)
    db.lane_status.insert_many(list(lane_status.values()))
    db.state_events.insert_many(events)
    db.weather.insert_many(weather)
    db.intents_weather.insert_one(i_wx)
    db.intents_safety.insert_one(i_stop)
    db.intents_procedures.insert_one(i_prop)
    db.decisions.insert_many([d_wx, d_stop, d_ok, d_prop])
    db.sim_runs.insert_one(
        {
            "_id": ObjectId(),
            "tower_id": TOWER,
            "scenario": "seed_demo",
            "kind": "normal",
            "note": "generated three-minute history",
            "started_at": t0,
            "ended_at": end,
        }
    )
    return {
        "db": db.name,
        "from": t0,
        "to": end,
        "positions": len(positions),
        "clips": len(clips),
        "objects": [o["_id"] for o in objects],
        "pending_decision_id": d_prop["_id"],
        "approved_decision_id": d_ok["_id"],
        "advisory_decision_id": d_wx["_id"],
        "stop_pending_decision_id": d_stop["_id"],
        "clip_ids": [c["_id"] for c in clips],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="scratch database to write (never the real ones)")
    ap.add_argument("--end", help="ISO time the history ends at (default: now)")
    args = ap.parse_args(argv)
    if args.db in config.PROTECTED_DBS:
        print(f"refusing to seed {args.db}: that is a real database", file=sys.stderr)
        return 2
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else None
    client = MongoClient(config.mongo_uri(), appname="holdshort-viz-seed", tz_aware=True)
    try:
        info = seed(client[args.db], end)
    finally:
        client.close()
    print(
        f"seeded {info['db']}: {info['from'].isoformat()} .. {info['to'].isoformat()}, "
        f"{info['positions']} positions, {info['clips']} clips, {len(info['objects'])} objects"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
