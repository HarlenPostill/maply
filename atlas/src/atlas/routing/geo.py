"""Small planar geometry helpers in a local metric projection.

The survey area is about 8 km across, so an equirectangular projection centred on
it is accurate to well under 0.1%. Lengths that matter for reporting come from the
database (EPSG:7856); these helpers place points, sides and gaps.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

EARTH_R = 6_371_008.8
XY = tuple[float, float]


class Projector:
    def __init__(self, lng0: float, lat0: float):
        self.lng0, self.lat0 = lng0, lat0
        self.kx = math.radians(1) * EARTH_R * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_R

    def xy(self, lng: float, lat: float) -> XY:
        return ((lng - self.lng0) * self.kx, (lat - self.lat0) * self.ky)

    def lnglat(self, x: float, y: float) -> list[float]:
        return [round(self.lng0 + x / self.kx, 7), round(self.lat0 + y / self.ky, 7)]

    def line(self, coords) -> list[XY]:
        return [self.xy(c[0], c[1]) for c in coords]


def dist(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def cumulative(line: list[XY]) -> list[float]:
    out = [0.0]
    for a, b in zip(line, line[1:]):
        out.append(out[-1] + dist(a, b))
    return out


@dataclass
class Located:
    distance: float      # from the point to the line
    along: float         # metres along the line to the closest point
    point: XY            # closest point
    cross: float         # > 0: point is left of the line direction, < 0: right
    clamped: bool        # closest point is an end of the line, with the point beyond it


def locate(line: list[XY], cum: list[float], p: XY) -> Located:
    best = None
    for i, (a, b) in enumerate(zip(line, line[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg2
        raw = t
        t = min(1.0, max(0.0, t))
        q = (a[0] + t * dx, a[1] + t * dy)
        d = dist(p, q)
        if best is None or d < best[0] - 1e-9:
            cross = dx * (p[1] - a[1]) - dy * (p[0] - a[0])
            clamped = (i == 0 and raw < 0) or (i == len(line) - 2 and raw > 1)
            best = (d, cum[i] + t * math.sqrt(seg2), q, cross, clamped)
    d, along, q, cross, clamped = best
    return Located(d, along, q, cross, clamped)


def point_at(line: list[XY], cum: list[float], along: float) -> XY:
    along = min(max(along, 0.0), cum[-1])
    i = min(max(bisect_right(cum, along) - 1, 0), len(line) - 2)
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg == 0 else (along - cum[i]) / seg
    a, b = line[i], line[i + 1]
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def substring(line: list[XY], cum: list[float], a: float, b: float) -> list[XY]:
    """The line between a and b metres; reversed when b < a."""
    lo, hi = sorted((a, b))
    pts = [point_at(line, cum, lo)]
    pts += [line[i] for i in range(len(line)) if lo < cum[i] < hi]
    pts.append(point_at(line, cum, hi))
    return pts if a <= b else pts[::-1]


def offset(line: list[XY], metres: float) -> list[XY]:
    """Offset to the left (positive) or right (negative) of the direction. For display only."""
    if len(line) < 2:
        return list(line)
    normals = []
    for a, b in zip(line, line[1:]):
        d = dist(a, b) or 1.0
        normals.append((-(b[1] - a[1]) / d, (b[0] - a[0]) / d))
    out = []
    for i, p in enumerate(line):
        ns = [normals[j] for j in (i - 1, i) if 0 <= j < len(normals)]
        nx, ny = sum(n[0] for n in ns), sum(n[1] for n in ns)
        norm = math.hypot(nx, ny) or 1.0
        out.append((p[0] + metres * nx / norm, p[1] + metres * ny / norm))
    return out


def heading_from_end(line: list[XY], cum: list[float], at_start: bool, probe: float = 5.0) -> float:
    """Angle (radians, counter-clockwise from east) of the line leaving one of its ends."""
    total = cum[-1]
    d = min(probe, total / 2) if total > 0 else 0
    if at_start:
        a, b = line[0], point_at(line, cum, d)
    else:
        a, b = line[-1], point_at(line, cum, total - d)
    if dist(a, b) < 1e-6:
        b = line[1] if at_start else line[-2]
    return math.atan2(b[1] - a[1], b[0] - a[0])


def _orient(a: XY, b: XY, c: XY) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p1: XY, p2: XY, q1: XY, q2: XY) -> bool:
    """Proper crossing: the segments intersect away from each other's ends."""
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    return d1 * d2 < 0 and d3 * d4 < 0


def in_ring(p: XY, ring: list[XY]) -> bool:
    inside = False
    x, y = p
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def in_multipolygon(p: XY, polygons: list[list[list[XY]]]) -> bool:
    for rings in polygons:
        if rings and in_ring(p, rings[0]) and not any(in_ring(p, hole) for hole in rings[1:]):
            return True
    return False


class SegmentGrid:
    """Grid index of line segments, each tagged with a payload."""

    def __init__(self, cell: float = 50.0):
        self.cell = cell
        self.cells: dict[tuple[int, int], list] = defaultdict(list)

    def _keys(self, a: XY, b: XY, pad: float = 0.0):
        c = self.cell
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for i in range(math.floor((x0 - pad) / c), math.floor((x1 + pad) / c) + 1):
            for j in range(math.floor((y0 - pad) / c), math.floor((y1 + pad) / c) + 1):
                yield i, j

    def add_line(self, line: list[XY], payload):
        for a, b in zip(line, line[1:]):
            for k in self._keys(a, b):
                self.cells[k].append((a, b, payload))

    def near(self, p: XY, radius: float):
        seen = set()
        for k in self._keys(p, p, radius):
            for item in self.cells.get(k, ()):
                if id(item) not in seen:
                    seen.add(id(item))
                    yield item

    def along(self, a: XY, b: XY):
        seen = set()
        for k in self._keys(a, b):
            for item in self.cells.get(k, ()):
                if id(item) not in seen:
                    seen.add(id(item))
                    yield item


def point_segment_distance(p: XY, a: XY, b: XY) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg2))
    return dist(p, (a[0] + t * dx, a[1] + t * dy))


def segment_distance(p1: XY, p2: XY, q1: XY, q2: XY) -> float:
    """Shortest distance between two segments (0 when they cross or touch, including at vertices)."""
    if segments_cross(p1, p2, q1, q2):
        return 0.0
    return min(point_segment_distance(p1, q1, q2), point_segment_distance(p2, q1, q2),
               point_segment_distance(q1, p1, p2), point_segment_distance(q2, p1, p2))


GAP_TERMINAL_M = 0.05


def gap_blocked(p: XY, q: XY, segments) -> bool:
    """Whether the straight gap p -> q meets any of the given (a, b) street segments.

    Touching counts as well as crossing, so a gap through a street's interior vertex
    or along a street is blocked. Only contact within GAP_TERMINAL_M of either end of
    the gap is allowed: the matched point on its own street or junction, and the
    requested point itself.
    """
    length = dist(p, q)
    if length <= 2 * GAP_TERMINAL_M:
        return False
    t = GAP_TERMINAL_M / length
    a = (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
    b = (q[0] - (q[0] - p[0]) * t, q[1] - (q[1] - p[1]) * t)
    return any(segment_distance(a, b, s1, s2) <= 1e-6 for s1, s2 in segments)
