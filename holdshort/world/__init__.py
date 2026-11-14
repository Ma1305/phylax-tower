"""World helpers: towers, lanes, hold points, controllers and streams, cached in-process.

Every helper takes the database first. Results are cached for ``CACHE_TTL_S`` seconds per
database; call ``clear_cache()`` after changing the world in tests.

    world(db, "LGA")                      -> {tower, lanes, hold_points, controllers,
                                              streams, points}
    lane_for_designator(db, "LGA", "04")  -> "RWY_04_22"
    lane_at(db, lon, lat)                 -> lane id or None (runway > taxiway > apron)
    owner_of(db, lane_id)                 -> controller id (supervisor when nobody owns it)
    streams_of(db, controller_id)         -> [stream id]
    station_of(db, stream_id)             -> "TOWER" | "GROUND" | ... | None
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

from pymongo.database import Database
from shapely.geometry import LineString, Point, shape
from shapely.geometry.polygon import orient

CACHE_TTL_S = 30.0
WORLD_DIR = Path(__file__).resolve().parent
DEFAULT_TOWER = "LGA"
_KIND_RANK = {"runway": 0, "taxiway": 1, "apron": 2}

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Any]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def load_world_file(tower_id: str = DEFAULT_TOWER) -> dict[str, Any]:
    """The seed file for a tower (holdshort/world/<tower>.json), or {} if there is none."""
    p = WORLD_DIR / f"{tower_id.lower()}.json"
    return json.loads(p.read_text()) if p.is_file() else {}


# ------------------------------------------------------------------- geometry


def lane_area(centerline: dict, width_m: float) -> dict:
    """GeoJSON Polygon: the centerline buffered by width_m / 2 (flat ends), CCW closed ring."""
    coords = centerline["coordinates"]
    lat0 = sum(c[1] for c in coords) / len(coords)
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 110_540.0
    local = LineString([(lon * kx, lat * ky) for lon, lat, *_ in coords])
    poly = orient(local.buffer(width_m / 2.0, cap_style="flat", join_style="mitre"), 1.0)
    ring = [[round(x / kx, 7), round(y / ky, 7)] for x, y in poly.exterior.coords]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


# ---------------------------------------------------------------------- cache


def _cached(db: Database, key: str, loader):
    k = (id(db.client), db.name, key)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(k)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    value = loader()
    with _lock:
        _cache[k] = (now, value)
    return value


def _all(db: Database) -> dict[str, list[dict]]:
    def load():
        return {
            "towers": list(db.towers.find()),
            "lanes": list(db.lanes.find()),
            "hold_points": list(db.hold_points.find()),
            "controllers": list(db.controllers.find()),
            "streams": list(db.streams.find()),
        }

    return _cached(db, "all", load)


def _shapes(db: Database) -> list[tuple[int, str, str, Any]]:
    def load():
        out = []
        for lane in _all(db)["lanes"]:
            if lane.get("area"):
                out.append((_KIND_RANK.get(lane.get("kind"), 9), lane["_id"],
                            lane.get("tower_id"), shape(lane["area"])))
        out.sort(key=lambda s: s[0])
        return out

    return _cached(db, "shapes", load)


# -------------------------------------------------------------------- helpers


def world(db: Database, tower_id: str = DEFAULT_TOWER) -> dict[str, Any]:
    """Everything the APIs need about one tower."""
    a = _all(db)

    def mine(docs):
        return [d for d in docs if d.get("tower_id") == tower_id]

    tower = next((t for t in a["towers"] if t["_id"] == tower_id), None)
    return {
        "tower": tower,
        "lanes": mine(a["lanes"]),
        "hold_points": mine(a["hold_points"]),
        "controllers": mine(a["controllers"]),
        "streams": mine(a["streams"]),
        "points": load_world_file(tower_id).get("points", {}),
    }


def lane_for_designator(db: Database, tower_id: str, designator: str | None) -> str | None:
    """Lane id for a runway ("04", "4", "22") or taxiway ("D") designator."""
    if not designator:
        return None
    d = str(designator).strip().upper()
    if d.isdigit() and len(d) == 1:
        d = "0" + d
    for lane in _all(db)["lanes"]:
        names = [x.upper() for x in lane.get("designators", [])]
        if lane.get("tower_id") == tower_id and d in names:
            return lane["_id"]
    return None


def lane_at(db: Database, lon: float, lat: float, tower_id: str | None = None) -> str | None:
    """The lane whose area contains (lon, lat); runway beats taxiway beats apron."""
    p = Point(lon, lat)
    for _rank, lane_id, t_id, geom in _shapes(db):
        if tower_id and t_id != tower_id:
            continue
        if geom.covers(p):
            return lane_id
    return None


def supervisor_of(db: Database, tower_id: str = DEFAULT_TOWER) -> str | None:
    for c in _all(db)["controllers"]:
        if c.get("tower_id") == tower_id and c.get("role") == "supervisor":
            return c["_id"]
    return None


def owner_of(db: Database, lane_id: str | None, tower_id: str | None = None) -> str | None:
    """The controller who owns lane_id; the supervisor when nobody does (or lane_id is None)."""
    a = _all(db)
    if lane_id:
        lane = next((x for x in a["lanes"] if x["_id"] == lane_id), None)
        if lane and not tower_id:
            tower_id = lane.get("tower_id")
        for c in a["controllers"]:
            if lane_id in c.get("lanes", []) and c.get("role") != "supervisor":
                return c["_id"]
    return supervisor_of(db, tower_id or DEFAULT_TOWER)


def streams_of(db: Database, controller_id: str) -> list[str]:
    """Stream ids a controller works (streams.controller_id, then controllers.streams)."""
    a = _all(db)
    ids = [s["_id"] for s in a["streams"] if s.get("controller_id") == controller_id]
    for c in a["controllers"]:
        if c["_id"] == controller_id:
            ids += [s for s in c.get("streams", []) if s not in ids]
    return ids


def stream_of(db: Database, controller_id: str) -> str | None:
    """The controller's first stream (where to speak), or None."""
    ids = streams_of(db, controller_id)
    return ids[0] if ids else None


def station_of(db: Database, stream_id: str | None) -> str | None:
    """The controller station heard on a stream ("TOWER", "GROUND", "OPS"), or None."""
    for s in _all(db)["streams"]:
        if s["_id"] == stream_id:
            return s.get("station")
    return None


def stream_doc(db: Database, stream_id: str) -> dict | None:
    return next((s for s in _all(db)["streams"] if s["_id"] == stream_id), None)


__all__ = [
    "CACHE_TTL_S", "clear_cache", "lane_area", "lane_at", "lane_for_designator",
    "load_world_file", "owner_of", "station_of", "stream_doc", "stream_of",
    "streams_of", "supervisor_of", "world",
]
