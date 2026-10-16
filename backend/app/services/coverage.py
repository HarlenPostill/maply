"""Coverage reporting.

Collection completeness (how many tasks reached a terminal state) is kept
separate from verified coverage (how many planned views have a verified
capture). Reused panoramas, uncertain associations, missing views and
no-imagery outcomes are reported explicitly. Gaps are measured from *actual*
camera positions projected onto each segment, not from requested positions.
"""

from __future__ import annotations

from collections import defaultdict

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.geo.projection import to_projected
from app.models import CapturePlan, Job, Observation, ObservationLink, PlannedView, Sample, Segment, Task

TERMINAL = {"verified", "needs_review", "no_imagery", "failed", "cancelled"}


def job_coverage(s: Session, job: Job) -> dict:
    plan = s.get(CapturePlan, job.plan_id)
    interval = float((plan.params or {}).get("sampling_interval_m", 15.0))
    segs = {sg.id: sg for sg in s.execute(select(Segment).where(Segment.plan_id == plan.id)).scalars()}
    samples = {sm.id: sm for sm in s.execute(select(Sample).where(Sample.plan_id == plan.id)).scalars()}
    tasks = {t.sample_id: t for t in s.execute(select(Task).where(Task.job_id == job.id)).scalars()}
    views = s.execute(select(PlannedView).where(PlannedView.plan_id == plan.id)).scalars().all()
    obs = {
        o.planned_view_id: o
        for o in s.execute(select(Observation).where(Observation.job_id == job.id, Observation.is_current.is_(True))).scalars()
    }
    obs_by_id = {o.id: o for o in obs.values()}
    links = {
        lk.planned_view_id: lk
        for lk in s.execute(
            select(ObservationLink).where(ObservationLink.job_id == job.id, ObservationLink.active.is_(True), ObservationLink.link_kind != "direct")
        ).scalars()
    }

    totals = defaultdict(int)
    assoc = defaultdict(int)
    per_sample: dict = defaultdict(lambda: defaultdict(int))
    side_status: dict = defaultdict(lambda: defaultdict(int))
    for v in views:
        smp = samples[v.sample_id]
        t = tasks.get(v.sample_id)
        if t is None:  # sample excluded from this job
            continue
        o = obs.get(v.id)
        if o is not None:
            st = "verified" if o.verification_status == "verified" else "needs_review"
            assoc[o.association_status] += 1
        elif v.id in links:
            st = "reused"
            assoc["reused_" + links[v.id].association_status] += 1
        elif t.status == "no_imagery":
            st = "no_imagery"
        elif t.status in TERMINAL or t.status == "captured":
            st = "missing"
        else:
            st = "pending"
        totals[st] += 1
        totals["planned_views"] += 1
        per_sample[v.sample_id][st] += 1
        side_status[(segs[smp.segment_id].segment_key, v.side)][st] += 1

    task_counts = defaultdict(int)
    for t in tasks.values():
        task_counts[t.status] += 1
    n_tasks = len(tasks)
    terminal = sum(n for k, n in task_counts.items() if k in TERMINAL)

    # sample features for the map
    def sample_status(counts: dict) -> str:
        for k in ("pending", "missing", "needs_review"):
            if counts.get(k):
                return k
        if counts.get("no_imagery"):
            return "no_imagery"
        if counts.get("reused") and not counts.get("verified"):
            return "reused"
        return "verified" if counts else "excluded"

    sample_features = []
    for sid, smp in samples.items():
        t = tasks.get(sid)
        counts = per_sample.get(sid, {})
        sample_features.append(
            {
                "type": "Feature",
                "geometry": mapping(to_shape(smp.requested_geom)),
                "properties": {
                    "id": str(sid),
                    "ordinal": smp.ordinal,
                    "kind": smp.kind,
                    "task_id": str(t.id) if t else None,
                    "task_status": t.status if t else "excluded",
                    "coverage": sample_status(counts) if t else "excluded",
                    "views": dict(counts),
                    "attempts": t.attempt_count if t else 0,
                    "reason": (t.outcome_reason or t.last_error) if t else None,
                },
            }
        )
    camera_features = []
    for o in obs.values():
        if o.camera_geom is not None:
            camera_features.append(
                {
                    "type": "Feature",
                    "geometry": mapping(to_shape(o.camera_geom)),
                    "properties": {
                        "observation_id": str(o.id),
                        "pano_id": o.pano_id,
                        "verification_status": o.verification_status,
                        "association_status": o.association_status,
                        "side": o.intended_side,
                        "heading_deg": o.heading_deg,
                        "label": "camera position reported by source (not a feature location)",
                    },
                }
            )

    # gaps from actual camera positions, per segment side
    gap_threshold = 1.5 * interval
    gaps = []
    cams: dict = defaultdict(list)
    for v in views:
        o = obs.get(v.id)
        if o is None and v.id in links:
            o = obs_by_id.get(links[v.id].observation_id)
        if o is None or o.camera_geom is None:
            continue
        smp = samples[v.sample_id]
        cams[(smp.segment_id, v.side)].append(o.camera_geom)
    for sg in segs.values():
        line = to_projected(to_shape(sg.geom))
        for side in sg.sides:
            pts = cams.get((sg.id, side), [])
            ch = sorted({round(line.project(to_projected(to_shape(g))), 1) for g in pts})
            edges = [0.0] + ch + [line.length]
            for a, b in zip(edges, edges[1:]):
                limit = gap_threshold / 2 if a == 0.0 or b == line.length else gap_threshold
                if b - a > limit:
                    gaps.append({"segment_key": sg.segment_key, "side": side, "from_m": round(a, 1), "to_m": round(b, 1), "length_m": round(b - a, 1)})
    panos = defaultdict(set)
    for o in obs.values():
        if o.pano_id:
            panos[o.pano_id].add(o.sample_id)
    shared = {p: len(sids) for p, sids in panos.items() if len(sids) > 1}
    reused_samples = len({links[v].sample_id for v in links})

    planned = totals.get("planned_views", 0) or 1
    return {
        "job_id": str(job.id),
        "collection": {
            "tasks_total": n_tasks,
            "tasks_terminal": terminal,
            "completeness": round(terminal / n_tasks, 4) if n_tasks else 0.0,
            "task_states": dict(task_counts),
        },
        "views": dict(totals),
        "verified_coverage": round(totals.get("verified", 0) / planned, 4),
        "covered_including_reuse_and_review": round((totals.get("verified", 0) + totals.get("reused", 0) + totals.get("needs_review", 0)) / planned, 4),
        "association": dict(assoc),
        "by_segment_side": [{"segment_key": k[0], "side": k[1], **dict(v)} for k, v in sorted(side_status.items())],
        "gaps": gaps,
        "gap_threshold_m": gap_threshold,
        "distinct_panoramas": len(panos),
        "panoramas_shared_by_multiple_samples": len(shared),
        "samples_resolved_by_reuse": reused_samples,
        "samples": {"type": "FeatureCollection", "features": sample_features},
        "cameras": {"type": "FeatureCollection", "features": camera_features},
        "notes": [
            "Collection completeness counts tasks that reached a terminal state; it is not coverage.",
            "Verified coverage counts planned views with a capture that passed automated verification.",
            "Association 'geometry_consistent' means camera offset and heading match the plan; footpath visibility is not visually confirmed.",
            "Camera points are positions reported by the imagery source, not locations of kerbs, ramps or obstructions.",
        ],
    }
