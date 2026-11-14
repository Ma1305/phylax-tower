"""The airport as the simulator sees it: lanes, hold points, named points, streams.

Built from the tower's ``GET /ingest/world`` answer, or from the bundled copy of
``docs/v2/world_lga.json`` (same shape) when working offline or as the stub tower.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path as FsPath
from typing import Any

from hssim.geo import XY, Frame, LonLat, Path, corridor, point_in_polygon

DATA = FsPath(__file__).resolve().parent / "data" / "world_lga.json"
_KIND_ORDER = {"runway": 0, "taxiway": 1, "apron": 2}
AIRBORNE_ON_RUNWAY_FT = 200.0


@dataclass
class Lane:
    id: str
    kind: str
    name: str
    designators: list[str]
    centerline: list[LonLat] | None
    width_m: float | None
    ring_xy: list[XY]
    ring: list[LonLat]


def _coords(geo: Any) -> list[LonLat]:
    return [(float(c[0]), float(c[1])) for c in geo]


class World:
    def __init__(self, data: dict[str, Any]):
        self.raw = data
        tower = data["tower"]
        self.tower_id: str = tower.get("_id") or tower.get("id") or "LGA"
        self.frame = Frame(tuple(tower["center"]["coordinates"]))  # type: ignore[arg-type]
        self.lanes: dict[str, Lane] = {}
        for ln in data["lanes"]:
            center = _coords(ln["centerline"]["coordinates"]) if ln.get("centerline") else None
            if ln.get("area"):
                ring = _coords(ln["area"]["coordinates"][0])
                ring_xy = [self.frame.to_xy(p) for p in ring]
            elif center and ln.get("width_m"):
                a, b = self.frame.to_xy(center[0]), self.frame.to_xy(center[-1])
                ring_xy = corridor(a, b, float(ln["width_m"]))
                ring = [self.frame.to_lonlat(p) for p in ring_xy]
            else:
                continue
            lid = ln.get("_id") or ln["id"]
            self.lanes[lid] = Lane(lid, ln["kind"], ln.get("name", lid),
                                   list(ln.get("designators", [])), center, ln.get("width_m"),
                                   ring_xy, ring)
        self.hold_points: dict[str, LonLat] = {}
        for hp in data.get("hold_points", []):
            self.hold_points[hp.get("_id") or hp["id"]] = tuple(hp["position"]["coordinates"])  # type: ignore[assignment]
        self.points: dict[str, LonLat] = {k: (float(v[0]), float(v[1]))
                                          for k, v in (data.get("points") or {}).items()}
        self.streams: dict[str, dict] = {(s.get("_id") or s["id"]): s
                                         for s in data.get("streams", [])}
        self.controllers: dict[str, dict] = {(c.get("_id") or c["id"]): c
                                             for c in data.get("controllers", [])}

    # ------------------------------------------------------------------ loading
    @classmethod
    def load_default(cls) -> World:
        return cls(json.loads(DATA.read_text()))

    # ------------------------------------------------------------------ lookups
    def point(self, name: str) -> LonLat:
        if name in self.points:
            return self.points[name]
        if name in self.hold_points:
            return self.hold_points[name]
        raise KeyError(f"unknown point {name!r}")

    def lane_at(self, lon: float, lat: float, status: str = "ground",
                alt_ft: float | None = None) -> str | None:
        """The lane a position is on (runway before taxiway before apron), or None.

        An airborne object at or above 200 ft is over the airfield, not on a lane.
        """
        if status == "airborne" and (alt_ft is None or alt_ft >= AIRBORNE_ON_RUNWAY_FT):
            return None
        p = self.frame.to_xy((lon, lat))
        hits = [ln for ln in self.lanes.values() if point_in_polygon(p, ln.ring_xy)]
        if not hits:
            return None
        return min(hits, key=lambda ln: _KIND_ORDER.get(ln.kind, 9)).id

    def path(self, *points: LonLat | str) -> Path:
        return Path(self.frame, [self.point(p) if isinstance(p, str) else p for p in points])

    def station(self, stream_id: str) -> str:
        return (self.streams.get(stream_id) or {}).get("station") or "TOWER"

    # --------------------------------------------------------- for the stub tower
    def as_ingest_world(self) -> dict[str, Any]:
        """The ``GET /ingest/world`` body: the raw world with every lane's area polygon."""
        out = json.loads(json.dumps(self.raw))
        out.pop("_comment", None)
        for ln in out["lanes"]:
            lane = self.lanes.get(ln.get("_id") or ln.get("id"))
            if lane and not ln.get("area"):
                ring = [[round(p[0], 7), round(p[1], 7)] for p in lane.ring]
                ln["area"] = {"type": "Polygon", "coordinates": [ring + [ring[0]]]}
        return out
