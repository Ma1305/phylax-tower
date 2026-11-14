"""Small flat-earth geometry helpers (good to well under a metre over an airfield).

Positions are (lon, lat) tuples in decimal degrees, the same order as GeoJSON.
Local x/y are metres east/north of a reference point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

LonLat = tuple[float, float]
XY = tuple[float, float]

M_PER_DEG_LAT = 111_320.0
NM = 1852.0
KT = 0.514444  # m/s per knot
FT = 0.3048


@dataclass(frozen=True)
class Frame:
    """A local tangent plane around ``ref``."""

    ref: LonLat

    @property
    def _kx(self) -> float:
        return M_PER_DEG_LAT * math.cos(math.radians(self.ref[1]))

    def to_xy(self, p: LonLat) -> XY:
        return ((p[0] - self.ref[0]) * self._kx, (p[1] - self.ref[1]) * M_PER_DEG_LAT)

    def to_lonlat(self, xy: XY) -> LonLat:
        return (self.ref[0] + xy[0] / self._kx, self.ref[1] + xy[1] / M_PER_DEG_LAT)

    def distance_m(self, a: LonLat, b: LonLat) -> float:
        ax, ay = self.to_xy(a)
        bx, by = self.to_xy(b)
        return math.hypot(bx - ax, by - ay)

    def bearing_deg(self, a: LonLat, b: LonLat) -> float:
        """Compass bearing from a to b (0 = north, 90 = east)."""
        ax, ay = self.to_xy(a)
        bx, by = self.to_xy(b)
        return math.degrees(math.atan2(bx - ax, by - ay)) % 360.0

    def offset(self, p: LonLat, bearing_deg: float, dist_m: float) -> LonLat:
        x, y = self.to_xy(p)
        b = math.radians(bearing_deg)
        return self.to_lonlat((x + math.sin(b) * dist_m, y + math.cos(b) * dist_m))

    def along(self, a: LonLat, b: LonLat, frac: float) -> LonLat:
        """The point ``frac`` of the way from a to b (may be <0 or >1 to extrapolate)."""
        return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


class Path:
    """A polyline you can walk along by distance."""

    def __init__(self, frame: Frame, points: list[LonLat]):
        if len(points) < 2:
            raise ValueError("a path needs at least two points")
        self.frame = frame
        self.points = list(points)
        self._xy = [frame.to_xy(p) for p in points]
        self._cum = [0.0]
        for (x0, y0), (x1, y1) in zip(self._xy, self._xy[1:]):
            self._cum.append(self._cum[-1] + math.hypot(x1 - x0, y1 - y0))

    @property
    def length_m(self) -> float:
        return self._cum[-1]

    def _segment(self, s: float) -> tuple[int, float]:
        s = max(0.0, min(self.length_m, s))
        for i in range(len(self._cum) - 1):
            if s <= self._cum[i + 1] or i == len(self._cum) - 2:
                seg = self._cum[i + 1] - self._cum[i]
                return i, 0.0 if seg == 0 else (s - self._cum[i]) / seg
        return len(self._cum) - 2, 1.0

    def at(self, s: float) -> LonLat:
        i, f = self._segment(s)
        (x0, y0), (x1, y1) = self._xy[i], self._xy[i + 1]
        return self.frame.to_lonlat((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))

    def heading_at(self, s: float) -> float:
        i, _ = self._segment(s)
        (x0, y0), (x1, y1) = self._xy[i], self._xy[i + 1]
        if x0 == x1 and y0 == y1:
            return 0.0
        return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360.0

    def distance_to_point(self, p: LonLat) -> float:
        """Distance along the path of the vertex-projected point closest to p."""
        px, py = self.frame.to_xy(p)
        best_s, best_d = 0.0, float("inf")
        for i, ((x0, y0), (x1, y1)) in enumerate(zip(self._xy, self._xy[1:])):
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            f = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg2))
            cx, cy = x0 + dx * f, y0 + dy * f
            d = math.hypot(px - cx, py - cy)
            if d < best_d:
                best_d, best_s = d, self._cum[i] + math.sqrt(seg2) * f
        return best_s


def point_in_polygon(p: XY, ring: list[XY]) -> bool:
    """Ray casting; ``ring`` may or may not repeat its first point."""
    x, y = p
    inside = False
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            if x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
    return inside


def corridor(a: XY, b: XY, width_m: float) -> list[XY]:
    """Rectangle of ``width_m`` around the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * width_m / 2, dx / length * width_m / 2
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny),
            (a[0] - nx, a[1] - ny)]
