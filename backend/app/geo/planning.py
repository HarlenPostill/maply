"""Deterministic capture-plan generation.

Input: operator-drawn route lines (and/or polygons) in WGS84.
Output: segments with stable keys, requested sample positions, proposed view
orientations, and intersection / crossing-approach nodes.

Everything here is pure geometry: no database, no network. Orientations are
*proposals* from the local route bearing and do not prove that the intended
footpath is visible from whichever panorama the source eventually serves.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from shapely.geometry import LineString, MultiLineString, Point, Polygon, shape
from shapely.geometry.polygon import orient
from shapely.ops import substring

from app.geo.projection import (
    DEFAULT_PROJECTED_EPSG,
    SIDE_LEFT,
    SIDE_NONE,
    SIDE_RIGHT,
    geodesic_azimuth,
    heading_delta,
    normalize_heading,
    offset_sign,
    perpendicular_heading,
    side_of_heading,
    to_projected,
    to_wgs84,
    within_area_of_use,
)

MAX_SAMPLES_PER_PLAN = 5000


class PlanParams(BaseModel):
    """Operator-reviewable planning parameters. Persisted with every plan."""

    sampling_interval_m: float = Field(15.0, ge=3.0, le=500.0)
    sides: Literal["left", "right", "both"] = "both"
    views_per_side: int = Field(1, ge=1, le=3, description="1=perpendicular, 2=+forward oblique, 3=+backward oblique")
    oblique_angle_deg: float = Field(45.0, ge=10.0, le=80.0)
    pitch_deg: float = Field(-5.0, ge=-90.0, le=90.0)
    fov_deg: float = Field(80.0, ge=10.0, le=100.0)
    viewport_width: int = Field(1440, ge=320, le=2560)
    viewport_height: int = Field(900, ge=240, le=1600)
    footpath_offset_m: float = Field(7.0, ge=1.0, le=40.0, description="Approximate centreline-to-footpath distance for planning overlays")
    segment_max_length_m: float = Field(120.0, ge=10.0, le=2000.0)
    turn_split_deg: float = Field(30.0, ge=5.0, le=170.0)
    include_crossing_approaches: bool = True
    include_route_endpoints_as_nodes: bool = True
    crossing_approach_offset_m: float = Field(10.0, ge=2.0, le=60.0)
    crossing_view_angle_deg: float = Field(35.0, ge=0.0, le=80.0)
    polygon_mode: Literal["perimeter", "clip_lines"] = "perimeter"
    projected_epsg: int = DEFAULT_PROJECTED_EPSG

    # capture/verification policy (reviewed with the plan, copied to jobs)
    max_imagery_age_months: int | None = Field(60, ge=1, le=600)
    unknown_date_policy: Literal["needs_review", "accept_flagged", "reject"] = "needs_review"
    max_camera_offset_m: float = Field(20.0, ge=1.0, le=200.0, description="Camera farther than this from the requested sample => association uncertain")
    no_imagery_distance_m: float = Field(60.0, ge=5.0, le=500.0, description="Camera farther than this => treated as no imagery near the sample")
    heading_tolerance_deg: float = Field(10.0, ge=1.0, le=90.0)
    reuse_panorama_views: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.no_imagery_distance_m < self.max_camera_offset_m:
            raise ValueError("no_imagery_distance_m must be >= max_camera_offset_m")
        return self


@dataclass
class ViewSpec:
    side: str
    view_kind: str
    heading_deg: float
    pitch_deg: float
    fov_deg: float
    viewport_w: int
    viewport_h: int
    orientation_basis: str = "local_route_bearing"


@dataclass
class SampleSpec:
    segment_key: str
    kind: str  # street | crossing_approach | seed
    chainage_m: float  # along the segment, from its stored start
    lon: float
    lat: float
    local_bearing_deg: float
    views: list[ViewSpec]
    node_key: str | None = None
    route_chainage_m: float = 0.0
    route_index: int = 0


@dataclass
class SegmentSpec:
    key: str
    route_index: int
    ordinal: int
    name: str
    coords: list[tuple[float, float]]  # WGS84 lon/lat in stored direction
    length_m: float
    bearing_deg: float
    footpath_left: list[tuple[float, float]] | None
    footpath_right: list[tuple[float, float]] | None
    sides: list[str]
    route_start_m: float = 0.0


@dataclass
class NodeSpec:
    key: str
    kind: str  # turn_vertex | route_crossing | route_endpoint | user_marked
    lon: float
    lat: float
    segment_keys: list[str] = field(default_factory=list)
    detection: str = "heuristic_candidate"


@dataclass
class PlanResult:
    segments: list[SegmentSpec]
    samples: list[SampleSpec]
    nodes: list[NodeSpec]
    warnings: list[str]
    summary: dict


class PlanningError(ValueError):
    pass


# --------------------------------------------------------------------------
# Stable keys
# --------------------------------------------------------------------------


def _round_coords(coords) -> str:
    return ";".join(f"{x:.6f},{y:.6f}" for x, y in coords)


def segment_key_for(coords_wgs84: list[tuple[float, float]]) -> str:
    """Deterministic ID from geometry in stored direction (~0.1 m rounding)."""
    return "seg_" + hashlib.sha1(_round_coords(coords_wgs84).encode()).hexdigest()[:12]


def node_key_for(lon: float, lat: float) -> str:
    return "node_" + hashlib.sha1(f"{lon:.6f},{lat:.6f}".encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Input normalisation
# --------------------------------------------------------------------------


def lines_from_geojson(geojson: dict, polygon_mode: str = "perimeter") -> tuple[list[tuple[LineString, str]], list[Polygon]]:
    """Return (lines, clip_polygons) from a Feature, FeatureCollection or geometry."""
    feats: list[tuple[dict, dict]] = []
    t = geojson.get("type")
    if t == "FeatureCollection":
        for f in geojson.get("features", []):
            feats.append((f.get("geometry") or {}, f.get("properties") or {}))
    elif t == "Feature":
        feats.append((geojson.get("geometry") or {}, geojson.get("properties") or {}))
    else:
        feats.append((geojson, {}))

    lines: list[tuple[LineString, str]] = []
    polys: list[Polygon] = []
    for geom, props in feats:
        if not geom:
            continue
        g = shape(geom)
        name = str(props.get("name") or props.get("street") or "")
        for part in getattr(g, "geoms", [g]):
            if isinstance(part, LineString):
                lines.append((part, name))
            elif isinstance(part, Polygon):
                if polygon_mode == "perimeter":
                    ring = orient(part, sign=1.0).exterior  # CCW: interior on the left
                    lines.append((LineString(ring.coords), name or "polygon perimeter"))
                else:
                    polys.append(part)
    if polys:
        clipped: list[tuple[LineString, str]] = []
        for ln, name in lines:
            for poly in polys:
                inter = ln.intersection(poly)
                for p in getattr(inter, "geoms", [inter]):
                    if isinstance(p, LineString) and not p.is_empty and p.length > 0:
                        clipped.append((p, name))
        lines = clipped
    return lines, polys


def _dedupe(coords: list[tuple[float, float]], tol: float = 0.05) -> list[tuple[float, float]]:
    out = [coords[0]]
    for c in coords[1:]:
        if math.dist(c, out[-1]) > tol:
            out.append(c)
    return out


def _planar_bearing(a, b) -> float:
    # Grid bearing; used only for *turn angles* between consecutive edges where
    # the convergence offset cancels out.
    return normalize_heading(math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])))


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------


def generate_plan(
    routes: list[tuple[LineString, str]],
    params: PlanParams,
    scope=None,
    seeds: list[dict] | None = None,
) -> PlanResult:
    """Generate a plan from WGS84 route lines.

    `scope` (shapely geometry, WGS84) is the approved boundary; any route vertex
    outside it is an error. `seeds` are parsed Street View observations
    ({lat,lng,heading,pitch,fov}) attached to the nearest segment.
    """
    epsg = params.projected_epsg
    warnings: list[str] = []
    if not routes:
        raise PlanningError("No route geometry supplied")

    for line, _ in routes:
        for lon, lat in line.coords:
            if not within_area_of_use(lon, lat, epsg):
                raise PlanningError(f"Coordinate {lon:.5f},{lat:.5f} is outside the area of use of EPSG:{epsg}")
        if scope is not None and not scope.buffer(1e-6).contains(line):
            raise PlanningError("Route extends outside the approved geographic scope")

    sides = [SIDE_LEFT, SIDE_RIGHT] if params.sides == "both" else [params.sides]
    segments: list[SegmentSpec] = []
    nodes: dict[str, NodeSpec] = {}
    samples: list[SampleSpec] = []
    seg_lines_proj: dict[str, LineString] = {}

    def add_node(x: float, y: float, kind: str, seg_keys: list[str]):
        lon, lat = to_wgs84(Point(x, y), epsg).coords[0]
        key = node_key_for(lon, lat)
        priority = {"route_crossing": 3, "turn_vertex": 2, "route_endpoint": 1, "user_marked": 4}
        if key in nodes:
            n = nodes[key]
            if priority.get(kind, 0) > priority.get(n.kind, 0):
                n.kind = kind
            for k in seg_keys:
                if k not in n.segment_keys:
                    n.segment_keys.append(k)
        else:
            nodes[key] = NodeSpec(key=key, kind=kind, lon=lon, lat=lat, segment_keys=list(seg_keys))
        return key

    ordinal = 0
    for ri, (line_wgs, name) in enumerate(routes):
        proj: LineString = to_projected(line_wgs, epsg)  # type: ignore[assignment]
        coords = _dedupe(list(proj.coords))
        if len(coords) < 2:
            warnings.append(f"Route {ri} has fewer than two distinct vertices; skipped")
            continue
        closed = math.dist(coords[0], coords[-1]) < 0.5 and len(coords) > 3

        # 1. split at sharp turns
        pieces: list[list[tuple[float, float]]] = []
        turn_points: list[tuple[float, float]] = []
        cur = [coords[0]]
        for i in range(1, len(coords) - 1):
            cur.append(coords[i])
            turn = abs(heading_delta(_planar_bearing(coords[i], coords[i + 1]), _planar_bearing(coords[i - 1], coords[i])))
            if turn > params.turn_split_deg:
                pieces.append(cur)
                turn_points.append(coords[i])
                cur = [coords[i]]
        cur.append(coords[-1])
        pieces.append(cur)

        # 2. split long pieces into near-equal parts
        seg_geoms: list[LineString] = []
        for pc in pieces:
            ls = LineString(pc)
            n = max(1, math.ceil(ls.length / params.segment_max_length_m))
            if n == 1:
                seg_geoms.append(ls)
            else:
                step = ls.length / n
                for k in range(n):
                    part = substring(ls, k * step, (k + 1) * step)
                    if isinstance(part, LineString) and part.length > 0:
                        seg_geoms.append(part)

        route_start = 0.0
        route_segs: list[SegmentSpec] = []
        for si, sg in enumerate(seg_geoms):
            wgs = to_wgs84(sg, epsg)
            wcoords = [(float(x), float(y)) for x, y in wgs.coords]
            key = segment_key_for(wcoords)
            if key in seg_lines_proj:
                warnings.append(f"Duplicate segment geometry {key} ignored")
                route_start += sg.length
                continue
            (lon0, lat0), (lon1, lat1) = wcoords[0], wcoords[-1]
            fp: dict[str, list | None] = {SIDE_LEFT: None, SIDE_RIGHT: None}
            for sd in (SIDE_LEFT, SIDE_RIGHT):
                off = sg.offset_curve(offset_sign(sd) * params.footpath_offset_m)
                if isinstance(off, MultiLineString):
                    off = max(off.geoms, key=lambda g: g.length)
                if isinstance(off, LineString) and not off.is_empty:
                    fp[sd] = [(float(x), float(y)) for x, y in to_wgs84(off, epsg).coords]
            spec = SegmentSpec(
                key=key,
                route_index=ri,
                ordinal=ordinal,
                name=f"{name or 'Route ' + str(ri + 1)} · {si + 1}",
                coords=wcoords,
                length_m=float(sg.length),
                bearing_deg=geodesic_azimuth(lon0, lat0, lon1, lat1),
                footpath_left=fp[SIDE_LEFT],
                footpath_right=fp[SIDE_RIGHT],
                sides=sides,
                route_start_m=route_start,
            )
            ordinal += 1
            segments.append(spec)
            route_segs.append(spec)
            seg_lines_proj[key] = sg
            route_start += sg.length

        if not route_segs:
            continue
        total = route_start

        # 3. nodes from turn vertices and endpoints
        for tp in turn_points:
            touching = [s.key for s in route_segs if _touches_end(seg_lines_proj[s.key], tp)]
            add_node(tp[0], tp[1], "turn_vertex", touching)
        if params.include_route_endpoints_as_nodes and not closed:
            add_node(coords[0][0], coords[0][1], "route_endpoint", [route_segs[0].key])
            add_node(coords[-1][0], coords[-1][1], "route_endpoint", [route_segs[-1].key])

        # 4. evenly spaced street samples along the whole route
        d = params.sampling_interval_m
        positions = [k * d for k in range(int(total // d) + 1)]
        if total - positions[-1] > d / 3.0 and not closed:
            positions.append(total)
        for c in positions:
            seg = _segment_at(route_segs, c)
            local_c = min(max(c - seg.route_start_m, 0.0), seg.length_m)
            sp = _make_street_sample(seg, seg_lines_proj[seg.key], local_c, params, sides, epsg)
            sp.route_chainage_m = c
            sp.route_index = ri
            samples.append(sp)

    # 5. crossings between different segments (excluding shared endpoints)
    keys = list(seg_lines_proj)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = seg_lines_proj[keys[i]], seg_lines_proj[keys[j]]
            if not a.intersects(b):
                continue
            inter = a.intersection(b)
            for p in getattr(inter, "geoms", [inter]):
                if not isinstance(p, Point):
                    continue
                shared_end = _touches_end(a, p.coords[0]) and _touches_end(b, p.coords[0])
                if shared_end:
                    continue
                add_node(p.x, p.y, "route_crossing", [keys[i], keys[j]])

    # 6. crossing approaches, recorded separately from street samples
    seg_by_key = {s.key: s for s in segments}
    if params.include_crossing_approaches:
        for node in nodes.values():
            npt = to_projected(Point(node.lon, node.lat), epsg)
            for sk in node.segment_keys:
                seg = seg_by_key[sk]
                ln = seg_lines_proj[sk]
                at = ln.project(npt)
                for direction in _approach_directions(at, ln.length):
                    off = min(params.crossing_approach_offset_m, ln.length / 2.0)
                    c = at - off if direction == "forward" else at + off
                    c = min(max(c, 0.0), ln.length)
                    sp = _make_approach_sample(seg, ln, c, direction, node.key, params, epsg)
                    sp.route_index = seg.route_index
                    sp.route_chainage_m = seg.route_start_m + c
                    samples.append(sp)

    # 7. Street View seeds become explicit samples on the nearest segment
    for seed in seeds or []:
        sp = _make_seed_sample(seed, segments, seg_lines_proj, params, epsg, warnings)
        if sp:
            samples.append(sp)

    samples.sort(key=lambda s: (s.route_index, s.route_chainage_m, s.kind != "street"))
    if len(samples) > MAX_SAMPLES_PER_PLAN:
        raise PlanningError(f"Plan would create {len(samples)} samples (limit {MAX_SAMPLES_PER_PLAN}); shorten the route or increase the interval")

    views = sum(len(s.views) for s in samples)
    summary = {
        "segments": len(segments),
        "samples": len(samples),
        "street_samples": sum(1 for s in samples if s.kind == "street"),
        "crossing_approach_samples": sum(1 for s in samples if s.kind == "crossing_approach"),
        "seed_samples": sum(1 for s in samples if s.kind == "seed"),
        "planned_views": views,
        "nodes": len(nodes),
        "total_length_m": round(sum(s.length_m for s in segments), 1),
        "projected_crs": f"EPSG:{epsg}",
        "note": "Orientations are proposals from local route bearing; visibility of the footpath is verified per capture.",
    }
    return PlanResult(segments=segments, samples=samples, nodes=list(nodes.values()), warnings=warnings, summary=summary)


def _touches_end(line: LineString, pt, tol: float = 0.5) -> bool:
    c = line.coords
    return math.dist(c[0], pt) < tol or math.dist(c[-1], pt) < tol


def _segment_at(segs: list[SegmentSpec], c: float) -> SegmentSpec:
    for s in segs:
        if c < s.route_start_m + s.length_m - 1e-9:
            return s
    return segs[-1]


def _bearing_at(line: LineString, c: float, epsg: int, window: float = 3.0) -> float:
    a = line.interpolate(max(0.0, c - window))
    b = line.interpolate(min(line.length, c + window))
    if a.distance(b) < 1e-6:
        a, b = line.interpolate(0.0), line.interpolate(line.length)
    (lon0, lat0) = to_wgs84(a, epsg).coords[0]
    (lon1, lat1) = to_wgs84(b, epsg).coords[0]
    return geodesic_azimuth(lon0, lat0, lon1, lat1)


def side_views(bearing: float, side: str, params: PlanParams) -> list[ViewSpec]:
    """Proposed views toward one side, relative to the stored segment bearing."""
    perp = perpendicular_heading(bearing, side)
    # Rotating a perpendicular view toward the segment's forward direction:
    # left side (b-90) -> b-90+angle; right side (b+90) -> b+90-angle.
    toward_fwd = params.oblique_angle_deg if side == SIDE_LEFT else -params.oblique_angle_deg
    kinds = [
        ("perpendicular", perp),
        ("oblique_forward", perp + toward_fwd),
        ("oblique_backward", perp - toward_fwd),
    ][: params.views_per_side]
    return [
        ViewSpec(
            side=side,
            view_kind=k,
            heading_deg=round(normalize_heading(h), 2),
            pitch_deg=params.pitch_deg,
            fov_deg=params.fov_deg,
            viewport_w=params.viewport_width,
            viewport_h=params.viewport_height,
        )
        for k, h in kinds
    ]


def _make_street_sample(seg: SegmentSpec, line: LineString, c: float, params: PlanParams, sides: list[str], epsg: int) -> SampleSpec:
    pt = to_wgs84(line.interpolate(c), epsg)
    b = _bearing_at(line, c, epsg)
    views: list[ViewSpec] = []
    for sd in sides:
        views.extend(side_views(b, sd, params))
    return SampleSpec(
        segment_key=seg.key,
        kind="street",
        chainage_m=round(c, 3),
        lon=pt.x,
        lat=pt.y,
        local_bearing_deg=round(b, 3),
        views=views,
    )


def _approach_directions(at: float, length: float, tol: float = 0.5) -> list[str]:
    """'forward' = approaching the node travelling in stored direction."""
    if at <= tol:
        return ["backward"]
    if at >= length - tol:
        return ["forward"]
    return ["forward", "backward"]


def _make_approach_sample(seg: SegmentSpec, line: LineString, c: float, direction: str, node_key: str, params: PlanParams, epsg: int) -> SampleSpec:
    pt = to_wgs84(line.interpolate(c), epsg)
    b = _bearing_at(line, c, epsg)
    ahead = b if direction == "forward" else normalize_heading(b + 180.0)
    a = params.crossing_view_angle_deg
    views = [
        ViewSpec(SIDE_NONE, "approach_ahead", round(ahead, 2), params.pitch_deg, params.fov_deg, params.viewport_width, params.viewport_height, "toward_node"),
    ]
    if a > 0:
        for kind, h in (("approach_ahead_left", ahead - a), ("approach_ahead_right", ahead + a)):
            h = normalize_heading(h)
            # Side label is relative to the STORED segment direction, not travel direction.
            views.append(
                ViewSpec(side_of_heading(h, b), kind, round(h, 2), params.pitch_deg, params.fov_deg, params.viewport_width, params.viewport_height, "toward_node_corner")
            )
    return SampleSpec(
        segment_key=seg.key,
        kind="crossing_approach",
        chainage_m=round(c, 3),
        lon=pt.x,
        lat=pt.y,
        local_bearing_deg=round(b, 3),
        views=views,
        node_key=node_key,
    )


def _make_seed_sample(seed: dict, segments: list[SegmentSpec], lines: dict[str, LineString], params: PlanParams, epsg: int, warnings: list[str]) -> SampleSpec | None:
    lat, lng = seed.get("lat"), seed.get("lng")
    if lat is None or lng is None or not segments:
        return None
    p = to_projected(Point(lng, lat), epsg)
    best = min(segments, key=lambda s: lines[s.key].distance(p))
    dist = lines[best.key].distance(p)
    if dist > 40:
        warnings.append(f"Street View seed at {lat:.6f},{lng:.6f} is {dist:.0f} m from the nearest segment; not attached")
        return None
    c = lines[best.key].project(p)
    b = _bearing_at(lines[best.key], c, epsg)
    heading = seed.get("heading")
    if heading is None:
        views = side_views(b, SIDE_LEFT, params) + side_views(b, SIDE_RIGHT, params)
    else:
        h = normalize_heading(float(heading))
        views = [
            ViewSpec(
                side_of_heading(h, b),
                "seed",
                round(h, 2),
                float(seed.get("pitch") if seed.get("pitch") is not None else params.pitch_deg),
                float(seed.get("fov") if seed.get("fov") is not None else params.fov_deg),
                params.viewport_width,
                params.viewport_height,
                "operator_street_view_url",
            )
        ]
    return SampleSpec(
        segment_key=best.key,
        kind="seed",
        chainage_m=round(c, 3),
        lon=float(lng),
        lat=float(lat),
        local_bearing_deg=round(b, 3),
        views=views,
        route_chainage_m=best.route_start_m + c,
        route_index=best.route_index,
    )


def recompute_sample_for_position(seg_line_wgs: LineString, lon: float, lat: float, params: PlanParams, sides: list[str], max_offset_m: float = 25.0) -> tuple[float, float, list[ViewSpec]]:
    """For an operator-moved sample: return (chainage, local bearing, views)."""
    epsg = params.projected_epsg
    line = to_projected(seg_line_wgs, epsg)
    p = to_projected(Point(lon, lat), epsg)
    dist = line.distance(p)
    if dist > max_offset_m:
        raise PlanningError(f"Sample moved {dist:.1f} m from its segment (max {max_offset_m} m)")
    c = line.project(p)
    b = _bearing_at(line, c, epsg)
    views: list[ViewSpec] = []
    for sd in sides:
        views.extend(side_views(b, sd, params))
    return c, b, views
