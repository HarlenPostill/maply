"""Turn streets + options into a list of shots, and write the job folder.

Each shot has a stable code (P007-L1), the documented Google Maps Street View
link for that exact position and view, and the file name Codex must save.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field
from shapely import STRtree
from shapely.geometry import LineString, Point, shape

from app.config import get_settings
from app.geo.planning import PlanParams, PlanningError, generate_plan
from app.geo.projection import to_projected
from app.geo.streetview_url import build_maps_pano_url

COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
SIDE_WORDS = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east", "S": "south", "SW": "south-west", "W": "west", "NW": "north-west"}


def compass(h: float) -> str:
    return COMPASS_16[int(((h % 360) + 11.25) // 22.5) % 16]


def compass8(h: float) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((h % 360) + 22.5) // 45) % 8]


class JobOptions(BaseModel):
    """The few choices that define a job. Everything else uses sensible defaults."""

    spacing_m: float = Field(15.0, ge=5.0, le=200.0, description="Distance between photo points along each street")
    sides: str = Field("both", pattern="^(both|left|right)$")
    angles: int = Field(1, ge=1, le=3, description="1 = straight across; 2 = + angled ahead; 3 = + angled behind")
    pitch: float = Field(-8.0, ge=-40.0, le=20.0)
    fov: float = Field(80.0, ge=30.0, le=100.0)
    corners: bool = Field(False, description="Extra photos looking into each intersection")
    batch_size: int = Field(20, ge=5, le=100)

    def plan_params(self) -> PlanParams:
        return PlanParams(
            sampling_interval_m=self.spacing_m,
            sides=self.sides,
            views_per_side=self.angles,
            pitch_deg=self.pitch,
            fov_deg=self.fov,
            include_crossing_approaches=self.corners,
            include_route_endpoints_as_nodes=self.corners,
            segment_max_length_m=max(120.0, self.spacing_m * 4),
        )


@dataclass
class ShotSpec:
    code: str
    ordinal: int
    batch: int
    segment_key: str
    street: str
    kind: str
    side: str
    view_kind: str
    lat: float
    lng: float
    heading: float
    pitch: float
    fov: float
    url: str
    filename: str  # relative to images/, e.g. "george-street/P00012-L1_-33.874278_151.205800_h277.jpg"
    look: str  # plain-language instruction, e.g. "west footpath of George Street, look W (277°)"
    footpath: str  # compass side of the street being photographed ("west"), or "" for intersection views
    near_node_m: float | None  # distance from the photo point to the nearest intersection/corner


_SUFFIX = {"perpendicular": "1", "oblique_forward": "2", "oblique_backward": "3"}
_CORNER = {"approach_ahead": "C", "approach_ahead_left": "CL", "approach_ahead_right": "CR"}


def describe(street: str, kind: str, view_kind: str, side_heading: float | None, heading: float) -> str:
    where = street or "the street"
    h = f"look {compass(heading)} ({heading:.0f}°)"
    if kind == "corner":
        turn = {"approach_ahead": "straight into the intersection", "approach_ahead_left": "at the left-hand corner", "approach_ahead_right": "at the right-hand corner"}[view_kind]
        return f"{where}, approaching the intersection, {turn}; {h}"
    side = SIDE_WORDS[compass8(side_heading if side_heading is not None else heading)]
    angle = {"perpendicular": "straight across", "oblique_forward": "angled along the footpath", "oblique_backward": "angled back along the footpath"}[view_kind]
    return f"{side} footpath of {where}, {angle}; {h}"


def plan_shots(streets: dict, options: JobOptions, scope=None) -> tuple[list[ShotSpec], list[dict], dict, list[str]]:
    """Return (shots, segments, summary, warnings) for a FeatureCollection of street lines."""
    warnings: list[str] = []
    lines: list[tuple[LineString, str]] = []
    metas: list[dict] = []
    outside: set[str] = set()
    for f in streets.get("features", []):
        g = shape(f["geometry"])
        props = f.get("properties") or {}
        name = props.get("name", "")
        meta = {"highway": props.get("highway", ""), "osm_way_ids": props.get("osm_way_ids", []), "osm_tags": props.get("osm_tags", {})}
        for part in getattr(g, "geoms", [g]):
            if not isinstance(part, LineString):
                continue
            pieces = [part]
            if scope is not None and not scope.contains(part):
                outside.add(name or "a street")
                inside = part.intersection(scope)
                pieces = [p for p in getattr(inside, "geoms", [inside]) if isinstance(p, LineString) and p.length > 0]
            for p in pieces:
                lines.append((p, name))
                metas.append(meta)
    if outside:
        warnings.append(f"{len(outside)} street{'s' if len(outside) > 1 else ''} cross the City of Sydney boundary; the parts outside were left out")
    if not lines:
        raise PlanningError("Add at least one street inside the area")
    plan = generate_plan(lines, options.plan_params())
    warnings += [w for w in plan.warnings if not w.startswith("Duplicate segment")]

    seg_route = {s.key: s.route_index for s in plan.segments}
    # distance from each photo point to the nearest intersection / corner node
    node_pts = [to_projected(Point(n.lon, n.lat)) for n in plan.nodes if n.kind != "route_endpoint"]
    node_tree = STRtree(node_pts) if node_pts else None

    n_points = len(plan.samples)
    width = max(3, len(str(n_points)))
    shots: list[ShotSpec] = []
    n = 0
    for i, sp in enumerate(plan.samples, start=1):
        street = lines[seg_route[sp.segment_key]][1]
        folder = slugify(street or "unnamed-street")
        kind = "corner" if sp.kind == "crossing_approach" else "street"
        near = None
        if node_tree is not None:
            p = to_projected(Point(sp.lon, sp.lat))
            near = round(p.distance(node_pts[int(node_tree.nearest(p))]), 1)
        for v in sp.views:
            if kind == "corner":
                suffix = _CORNER.get(v.view_kind, "C")
                side_heading = None
                footpath = ""
            else:
                suffix = ("L" if v.side == "left" else "R") + _SUFFIX.get(v.view_kind, "1")
                side_heading = sp.local_bearing_deg + (-90 if v.side == "left" else 90)
                footpath = SIDE_WORDS[compass8(side_heading)]
            code = f"P{i:0{width}d}-{suffix}"
            n += 1
            shots.append(
                ShotSpec(
                    code=code,
                    ordinal=n,
                    batch=(n - 1) // options.batch_size + 1,
                    segment_key=sp.segment_key,
                    street=street,
                    kind=kind,
                    side=v.side,
                    view_kind=v.view_kind,
                    lat=round(sp.lat, 7),
                    lng=round(sp.lon, 7),
                    heading=round(v.heading_deg, 1),
                    pitch=v.pitch_deg,
                    fov=v.fov_deg,
                    url=build_maps_pano_url(sp.lat, sp.lon, v.heading_deg, v.pitch_deg, v.fov_deg),
                    filename=f"{folder}/{code}_{sp.lat:.6f}_{sp.lon:.6f}_h{round(v.heading_deg) % 360:03d}.jpg",
                    look=describe(street, kind, v.view_kind, side_heading, v.heading_deg),
                    footpath=footpath,
                    near_node_m=near,
                )
            )
    segments = [
        {"key": s.key, "ordinal": s.ordinal, "street": lines[s.route_index][1], "coords": s.coords, "bearing_deg": s.bearing_deg,
         "length_m": s.length_m, "osm": metas[s.route_index]}
        for s in plan.segments
    ]
    summary = {
        "shots": len(shots),
        "points": n_points,
        "streets": len({nm for _, nm in lines if nm}) or len(lines),
        "length_m": round(sum(s.length_m for s in plan.segments)),
        "batches": (len(shots) - 1) // options.batch_size + 1 if shots else 0,
        "intersections": sum(1 for nd in plan.nodes if nd.kind != "route_endpoint"),
    }
    return shots, segments, summary, warnings


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "job"


def write_job_folder(slug: str, name: str, options: JobOptions, shots: list[ShotSpec], summary: dict | None = None) -> None:
    """Write the plain files Codex (and you) can read without the app."""
    root = get_settings().jobs_dir / slug
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    (root / "job.json").write_text(json.dumps({"name": name, "options": options.model_dump(), "summary": summary or {}, "shots_file": "shots.csv"}, indent=2))
    with open(root / "shots.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["shot", "batch", "street", "footpath", "look", "lat", "lng", "heading", "pitch", "fov", "near_intersection_m", "link", "save_as"])
        for s in shots:
            w.writerow([s.code, s.batch, s.street, s.footpath, s.look, s.lat, s.lng, s.heading, s.pitch, s.fov, s.near_node_m, s.url, f"images/{s.filename}"])
