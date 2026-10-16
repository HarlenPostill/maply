"""Persist, serialise and edit capture plans."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import LineString, Point, mapping
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.geo.planning import (
    PlanParams,
    PlanningError,
    generate_plan,
    lines_from_geojson,
    recompute_sample_for_position,
    side_views,
)
from app.geo.projection import normalize_heading, side_of_heading
from app.geo.streetview_url import parse_streetview_url
from app.models import CapturePlan, Job, PlanNode, PlannedView, Project, Route, Sample, Segment
from app.scheduler.jobs import snapshot_config


def create_plan(s: Session, project: Project, route_ids: list[uuid.UUID], params: PlanParams, seed_urls: list[str], name: str = "") -> CapturePlan:
    routes = s.execute(select(Route).where(Route.id.in_(route_ids), Route.project_id == project.id)).scalars().all()
    if len(routes) != len(set(route_ids)):
        raise PlanningError("One or more routes were not found in this project")
    lines: list[tuple[LineString, str]] = []
    line_route_ids: list[uuid.UUID] = []
    seeds: list[dict] = []
    for r in routes:
        g = to_shape(r.geom)
        if r.kind == "streetview_seed" or g.geom_type == "Point":
            seeds.append({"lat": g.y, "lng": g.x, **{k: r.properties.get(k) for k in ("heading", "pitch", "fov", "pano_id")}})
            continue
        ls, _ = lines_from_geojson(mapping(g), params.polygon_mode)
        lines.extend((ln, r.name or nm) for ln, nm in ls)
        line_route_ids.extend(r.id for _ in ls)
    for url in seed_urls:
        p = parse_streetview_url(url)
        if p.lat is None:
            raise PlanningError(f"Could not read a position from Street View URL: {url[:80]}")
        seeds.append({"lat": p.lat, "lng": p.lng, "heading": p.heading, "pitch": p.pitch, "fov": p.fov, "pano_id": p.pano_id})

    result = generate_plan(lines, params, scope=to_shape(project.scope_geom), seeds=seeds)
    cv = snapshot_config(s, project.id, "plan", {"params": params.model_dump(), "route_ids": [str(r) for r in route_ids], "seed_urls": seed_urls})
    plan = CapturePlan(
        project_id=project.id,
        name=name or f"Plan {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
        status="draft",
        params=params.model_dump(),
        route_ids=[str(r) for r in route_ids],
        config_version_id=cv.id,
        summary={**result.summary, "warnings": result.warnings},
    )
    s.add(plan)
    s.flush()

    route_by_index = dict(enumerate(line_route_ids))
    seg_ids: dict[str, uuid.UUID] = {}
    seg_rows = []
    for sg in result.segments:
        sid = uuid.uuid4()
        seg_ids[sg.key] = sid
        seg_rows.append(
            {
                "id": sid,
                "plan_id": plan.id,
                "route_id": route_by_index.get(sg.route_index),
                "segment_key": sg.key,
                "ordinal": sg.ordinal,
                "name": sg.name,
                "geom": from_shape(LineString(sg.coords), srid=4326),
                "length_m": sg.length_m,
                "bearing_deg": sg.bearing_deg,
                "footpath_left_geom": from_shape(LineString(sg.footpath_left), srid=4326) if sg.footpath_left and len(sg.footpath_left) > 1 else None,
                "footpath_right_geom": from_shape(LineString(sg.footpath_right), srid=4326) if sg.footpath_right and len(sg.footpath_right) > 1 else None,
                "sides": sg.sides,
            }
        )
    if seg_rows:
        s.execute(insert(Segment), seg_rows)
    node_ids: dict[str, uuid.UUID] = {}
    node_rows = []
    for n in result.nodes:
        nid = uuid.uuid4()
        node_ids[n.key] = nid
        node_rows.append({"id": nid, "plan_id": plan.id, "node_key": n.key, "kind": n.kind, "geom": from_shape(Point(n.lon, n.lat), srid=4326), "segment_keys": n.segment_keys, "detection": n.detection})
    if node_rows:
        s.execute(insert(PlanNode), node_rows)
    sample_rows, view_rows = [], []
    seq: dict[str, int] = {}
    for i, sp in enumerate(result.samples):
        smp_id = uuid.uuid4()
        seq[sp.segment_key] = seq.get(sp.segment_key, 0) + 1
        pt = from_shape(Point(sp.lon, sp.lat), srid=4326)
        sample_rows.append(
            {
                "id": smp_id,
                "plan_id": plan.id,
                "segment_id": seg_ids[sp.segment_key],
                "node_id": node_ids.get(sp.node_key) if sp.node_key else None,
                "ordinal": i + 1,
                "seq_in_segment": seq[sp.segment_key],
                "kind": sp.kind,
                "chainage_m": sp.chainage_m,
                "requested_geom": pt,
                "original_geom": pt,
                "local_bearing_deg": sp.local_bearing_deg,
                "edited": False,
                "excluded": False,
                "note": "",
            }
        )
        for j, v in enumerate(sp.views):
            view_rows.append(
                {
                    "id": uuid.uuid4(),
                    "plan_id": plan.id,
                    "sample_id": smp_id,
                    "ordinal": j + 1,
                    "side": v.side,
                    "view_kind": v.view_kind,
                    "heading_deg": v.heading_deg,
                    "pitch_deg": v.pitch_deg,
                    "fov_deg": v.fov_deg,
                    "viewport_w": v.viewport_w,
                    "viewport_h": v.viewport_h,
                    "orientation_basis": v.orientation_basis,
                }
            )
    if sample_rows:
        s.execute(insert(Sample), sample_rows)
    if view_rows:
        s.execute(insert(PlannedView), view_rows)
    return plan


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def plan_geojson(s: Session, plan: CapturePlan) -> dict:
    segs = s.execute(select(Segment).where(Segment.plan_id == plan.id).order_by(Segment.ordinal)).scalars().all()
    seg_key = {sg.id: sg.segment_key for sg in segs}
    seg_f, fp_f = [], []
    for sg in segs:
        seg_f.append(
            {
                "type": "Feature",
                "geometry": mapping(to_shape(sg.geom)),
                "properties": {"id": str(sg.id), "segment_key": sg.segment_key, "ordinal": sg.ordinal, "name": sg.name, "length_m": round(sg.length_m, 1), "bearing_deg": round(sg.bearing_deg, 2), "sides": sg.sides},
            }
        )
        for side, g in (("left", sg.footpath_left_geom), ("right", sg.footpath_right_geom)):
            if g is not None and side in sg.sides:
                fp_f.append({"type": "Feature", "geometry": mapping(to_shape(g)), "properties": {"segment_key": sg.segment_key, "side": side, "kind": "approximate_footpath_offset"}})
    views_by_sample: dict[uuid.UUID, list[dict]] = {}
    for v in s.execute(select(PlannedView).where(PlannedView.plan_id == plan.id).order_by(PlannedView.ordinal)).scalars():
        views_by_sample.setdefault(v.sample_id, []).append(
            {"id": str(v.id), "side": v.side, "view_kind": v.view_kind, "heading_deg": v.heading_deg, "pitch_deg": v.pitch_deg, "fov_deg": v.fov_deg, "basis": v.orientation_basis}
        )
    nodes = s.execute(select(PlanNode).where(PlanNode.plan_id == plan.id)).scalars().all()
    node_key = {n.id: n.node_key for n in nodes}
    smp_f = []
    for smp in s.execute(select(Sample).where(Sample.plan_id == plan.id).order_by(Sample.ordinal)).scalars():
        smp_f.append(
            {
                "type": "Feature",
                "geometry": mapping(to_shape(smp.requested_geom)),
                "properties": {
                    "id": str(smp.id),
                    "ordinal": smp.ordinal,
                    "kind": smp.kind,
                    "segment_key": seg_key.get(smp.segment_id),
                    "seq_in_segment": smp.seq_in_segment,
                    "chainage_m": round(smp.chainage_m, 2),
                    "local_bearing_deg": round(smp.local_bearing_deg, 2),
                    "edited": smp.edited,
                    "excluded": smp.excluded,
                    "note": smp.note,
                    "node_key": node_key.get(smp.node_id) if smp.node_id else None,
                    "views": views_by_sample.get(smp.id, []),
                },
            }
        )
    node_f = [
        {"type": "Feature", "geometry": mapping(to_shape(n.geom)), "properties": {"id": str(n.id), "node_key": n.node_key, "kind": n.kind, "detection": n.detection, "segment_keys": n.segment_keys}}
        for n in nodes
    ]
    return {"segments": _fc(seg_f), "footpaths": _fc(fp_f), "samples": _fc(smp_f), "nodes": _fc(node_f)}


def plan_dict(plan: CapturePlan) -> dict:
    return {
        "id": str(plan.id),
        "project_id": str(plan.project_id),
        "name": plan.name,
        "status": plan.status,
        "params": plan.params,
        "route_ids": plan.route_ids,
        "summary": plan.summary,
        "config_version_id": str(plan.config_version_id) if plan.config_version_id else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
    }


def ensure_editable(s: Session, plan: CapturePlan) -> None:
    if plan.status != "draft":
        raise PlanningError("Only draft plans can be edited; approved plans are immutable (create a new plan)")
    if s.execute(select(Job.id).where(Job.plan_id == plan.id)).first():
        raise PlanningError("Plan already has jobs")


def update_sample(s: Session, smp: Sample, *, lng=None, lat=None, excluded=None, note=None, reset=False) -> Sample:
    plan = s.get(CapturePlan, smp.plan_id)
    ensure_editable(s, plan)
    params = PlanParams(**plan.params)
    if excluded is not None:
        smp.excluded = excluded
    if note is not None:
        smp.note = note
    if reset:
        lng, lat = to_shape(smp.original_geom).x, to_shape(smp.original_geom).y
    if lng is not None and lat is not None:
        seg = s.get(Segment, smp.segment_id)
        line = to_shape(seg.geom)
        sides = seg.sides
        c, b, views = recompute_sample_for_position(line, lng, lat, params, sides)
        smp.requested_geom = from_shape(Point(lng, lat), srid=4326)
        smp.chainage_m = c
        smp.local_bearing_deg = b
        smp.edited = not reset
        old = s.execute(select(PlannedView).where(PlannedView.sample_id == smp.id).order_by(PlannedView.ordinal)).scalars().all()
        if smp.kind == "street":
            new_specs = views
        elif smp.kind == "crossing_approach":
            # keep approach direction (ahead heading relative to stored direction), re-derive from new bearing
            ahead_old = next((v for v in old if v.view_kind == "approach_ahead"), None)
            forward = True if ahead_old is None else abs(((ahead_old.heading_deg - b + 180) % 360) - 180) < 90
            ahead = b if forward else normalize_heading(b + 180)
            a = params.crossing_view_angle_deg
            from app.geo.planning import ViewSpec

            new_specs = [ViewSpec("none", "approach_ahead", round(ahead, 2), params.pitch_deg, params.fov_deg, params.viewport_width, params.viewport_height, "toward_node")]
            for kind, hh in (("approach_ahead_left", ahead - a), ("approach_ahead_right", ahead + a)):
                hh = normalize_heading(hh)
                new_specs.append(ViewSpec(side_of_heading(hh, b), kind, round(hh, 2), params.pitch_deg, params.fov_deg, params.viewport_width, params.viewport_height, "toward_node_corner"))
        else:
            new_specs = None  # seed views keep the operator's URL orientation
        if new_specs is not None:
            s.execute(delete(PlannedView).where(PlannedView.sample_id == smp.id))
            for j, v in enumerate(new_specs):
                s.add(
                    PlannedView(
                        plan_id=plan.id, sample_id=smp.id, ordinal=j + 1, side=v.side, view_kind=v.view_kind, heading_deg=v.heading_deg,
                        pitch_deg=v.pitch_deg, fov_deg=v.fov_deg, viewport_w=v.viewport_w, viewport_h=v.viewport_h,
                        orientation_basis=v.orientation_basis + ("_edited_position" if not reset else ""),
                    )
                )
    return smp


def add_views_for_side(params: PlanParams, bearing: float, side: str):
    return side_views(bearing, side, params)
