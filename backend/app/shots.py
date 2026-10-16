"""Turn streets + options into a list of shots, and write the job folder.

Each shot has a stable code (P007-L1), the documented Google Maps Street View
link for that exact position and view, and the file name Codex must save.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field
from shapely.geometry import LineString, shape

from app.config import get_settings
from app.geo.planning import PlanParams, PlanningError, generate_plan
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
    filename: str
    look: str  # plain-language instruction, e.g. "west footpath of George Street, look W (277°)"


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
    for f in streets.get("features", []):
        g = shape(f["geometry"])
        name = (f.get("properties") or {}).get("name", "")
        for part in getattr(g, "geoms", [g]):
            if not isinstance(part, LineString):
                continue
            if scope is not None and not scope.contains(part):
                inside = part.intersection(scope)
                warnings.append(f"Part of {name or 'a street'} is outside the City of Sydney boundary and was left out")
                for p in getattr(inside, "geoms", [inside]):
                    if isinstance(p, LineString) and p.length > 0:
                        lines.append((p, name))
                continue
            lines.append((part, name))
    if not lines:
        raise PlanningError("Add at least one street inside the area")
    plan = generate_plan(lines, options.plan_params())
    warnings += plan.warnings
    route_names = [n for _, n in lines]
    seg_street = {s.key: route_names[s.route_index] for s in plan.segments}

    shots: list[ShotSpec] = []
    n = 0
    for i, sp in enumerate(plan.samples, start=1):
        street = seg_street.get(sp.segment_key, "")
        kind = "corner" if sp.kind == "crossing_approach" else "street"
        for v in sp.views:
            if kind == "corner":
                suffix = _CORNER.get(v.view_kind, "C")
                side_heading = None
            else:
                suffix = ("L" if v.side == "left" else "R") + _SUFFIX.get(v.view_kind, "1")
                side_heading = sp.local_bearing_deg + (-90 if v.side == "left" else 90)
            code = f"P{i:03d}-{suffix}"
            n += 1
            filename = f"{code}_{sp.lat:.6f}_{sp.lon:.6f}_h{round(v.heading_deg) % 360:03d}.png"
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
                    filename=filename,
                    look=describe(street, kind, v.view_kind, side_heading, v.heading_deg),
                )
            )
    segments = [
        {"key": s.key, "ordinal": s.ordinal, "street": seg_street.get(s.key, ""), "coords": s.coords, "bearing_deg": s.bearing_deg, "length_m": s.length_m}
        for s in plan.segments
    ]
    summary = {
        "shots": len(shots),
        "points": len(plan.samples),
        "streets": len({x for x in route_names if x}) or len(lines),
        "length_m": round(sum(s.length_m for s in plan.segments)),
        "batches": (len(shots) - 1) // options.batch_size + 1 if shots else 0,
    }
    return shots, segments, summary, warnings


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "job"


def write_job_folder(slug: str, name: str, options: JobOptions, shots: list[ShotSpec]) -> None:
    """Write the plain files Codex (and you) can read without the app."""
    root = get_settings().jobs_dir / slug
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    (root / "job.json").write_text(json.dumps({"name": name, "options": options.model_dump(), "shots": [asdict(s) for s in shots]}, indent=2))
    with open(root / "shots.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["shot", "batch", "street", "look", "lat", "lng", "heading", "pitch", "fov", "link", "save_as"])
        for s in shots:
            w.writerow([s.code, s.batch, s.street, s.look, s.lat, s.lng, s.heading, s.pitch, s.fov, s.url, f"images/{s.filename}"])
