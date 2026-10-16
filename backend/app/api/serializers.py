from __future__ import annotations

from datetime import datetime, timezone

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EgressEvent, Job, ModelUsage, Observation, PlannedView, Project, Route, Sample, Task, Worker
from app.scheduler.jobs import accepted_capture_count, job_runtime_s, task_counts


def iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def file_url(rel: str | None, bust: datetime | None = None) -> str | None:
    if not rel:
        return None
    return f"/files/{rel}" + (f"?t={int(bust.timestamp() * 1000)}" if bust else "")


def project_dict(p: Project) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "config": p.config,
        "scope": mapping(to_shape(p.scope_geom)),
        "created_at": iso(p.created_at),
    }


def route_dict(r: Route) -> dict:
    return {"id": str(r.id), "project_id": str(r.project_id), "name": r.name, "kind": r.kind, "geometry": mapping(to_shape(r.geom)), "properties": r.properties, "created_at": iso(r.created_at)}


def worker_dict(s: Session, w: Worker) -> dict:
    now = datetime.now(timezone.utc)
    cur = None
    if w.current_task_id:
        t = s.get(Task, w.current_task_id)
        if t:
            smp = s.get(Sample, t.sample_id)
            views = s.execute(select(PlannedView).where(PlannedView.sample_id == t.sample_id).order_by(PlannedView.ordinal)).scalars().all()
            captured = {
                str(o.planned_view_id): o.verification_status
                for o in s.execute(select(Observation).where(Observation.task_id == t.id, Observation.is_current.is_(True))).scalars()
            }
            pt = to_shape(smp.requested_geom)
            cur = {
                "task_id": str(t.id),
                "ordinal": t.ordinal,
                "status": t.status,
                "segment_key": t.segment_key,
                "attempt": t.attempt_count,
                "sample": {"id": str(smp.id), "kind": smp.kind, "lat": pt.y, "lng": pt.x},
                "lease_expires_at": iso(t.lease_expires_at),
                "reason": t.outcome_reason,
                "views": [
                    {"id": str(v.id), "side": v.side, "view_kind": v.view_kind, "heading_deg": v.heading_deg, "captured": captured.get(str(v.id))}
                    for v in views
                ],
            }
    return {
        "id": w.id,
        "job_id": str(w.job_id) if w.job_id else None,
        "slot": w.slot,
        "host": w.host,
        "pid": w.pid,
        "status": w.status,
        "method": w.method,
        "message": w.message,
        "heartbeat_at": iso(w.heartbeat_at),
        "heartbeat_age_s": round((now - w.heartbeat_at).total_seconds(), 1) if w.heartbeat_at else None,
        "started_at": iso(w.started_at),
        "latest_screenshot_url": file_url(w.latest_screenshot_path, w.latest_screenshot_at),
        "latest_screenshot_at": iso(w.latest_screenshot_at),
        "browser_profile": w.browser_profile_dir,
        "current_task": cur,
    }


def job_dict(s: Session, j: Job, with_workers: bool = True) -> dict:
    usage = s.execute(
        select(
            func.count(ModelUsage.id),
            func.coalesce(func.sum(ModelUsage.input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cached_input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.output_tokens), 0),
            func.coalesce(func.sum(ModelUsage.images_sent), 0),
            func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0.0),
        ).where(ModelUsage.job_id == j.id)
    ).one()
    egress = dict(s.execute(select(EgressEvent.decision, func.count()).where(EgressEvent.job_id == j.id).group_by(EgressEvent.decision)).all())
    attempts = s.execute(select(func.coalesce(func.sum(Task.attempt_count), 0)).where(Task.job_id == j.id)).scalar_one()
    out = {
        "id": str(j.id),
        "project_id": str(j.project_id),
        "plan_id": str(j.plan_id),
        "config_version_id": str(j.config_version_id),
        "name": j.name,
        "status": j.status,
        "target": j.target,
        "method": j.method,
        "worker_count": j.worker_count,
        "requests_per_minute": j.requests_per_minute,
        "max_captures": j.max_captures,
        "max_duration_s": j.max_duration_s,
        "model_budget_usd": j.model_budget_usd,
        "runtime_s": round(job_runtime_s(j), 1),
        "pause_kind": j.pause_kind,
        "pause_reason": j.pause_reason,
        "settings": j.settings,
        "created_at": iso(j.created_at),
        "started_at": iso(j.started_at),
        "finished_at": iso(j.finished_at),
        "task_counts": task_counts(s, j.id),
        "attempts_total": int(attempts),
        "captures_current": accepted_capture_count(s, j.id),
        "model_usage": {
            "calls": usage[0],
            "input_tokens": int(usage[1]),
            "cached_input_tokens": int(usage[2]),
            "output_tokens": int(usage[3]),
            "images_sent": int(usage[4]),
            "estimated_cost_usd": round(float(usage[5]), 4),
            "label": "Estimate from configured prices; actual token usage as reported by the provider.",
        },
        "egress": {"allowed": egress.get("allowed", 0), "blocked": egress.get("blocked", 0)},
    }
    if with_workers:
        ws = s.execute(select(Worker).where(Worker.job_id == j.id).order_by(Worker.slot)).scalars().all()
        out["workers"] = [worker_dict(s, w) for w in ws]
    return out


def task_dict(t: Task) -> dict:
    return {
        "id": str(t.id),
        "job_id": str(t.job_id),
        "sample_id": str(t.sample_id),
        "segment_key": t.segment_key,
        "ordinal": t.ordinal,
        "status": t.status,
        "attempt_count": t.attempt_count,
        "failure_count": t.failure_count,
        "max_attempts": t.max_attempts,
        "next_attempt_at": iso(t.next_attempt_at),
        "lease_owner": t.lease_owner,
        "lease_expires_at": iso(t.lease_expires_at),
        "last_error": t.last_error,
        "outcome_reason": t.outcome_reason,
        "result": t.result,
        "view_filter": t.view_filter,
        "started_at": iso(t.started_at),
        "finished_at": iso(t.finished_at),
    }


def observation_summary(o: Observation, sample_ordinal: int | None = None) -> dict:
    return {
        "id": str(o.id),
        "job_id": str(o.job_id),
        "task_id": str(o.task_id),
        "sample_id": str(o.sample_id),
        "sample_ordinal": sample_ordinal,
        "segment_key": o.segment_key,
        "intended_side": o.intended_side,
        "view_kind": o.view_kind,
        "planned_heading_deg": o.planned_heading_deg,
        "heading_deg": o.heading_deg,
        "pano_id": o.pano_id,
        "imagery_date": o.imagery_date,
        "imagery_date_precision": o.imagery_date_precision,
        "camera_offset_m": o.camera_offset_m,
        "verification_status": o.verification_status,
        "association_status": o.association_status,
        "quality_flags": list(o.quality_flags or []),
        "review_status": o.review_status,
        "is_current": o.is_current,
        "capture_method": o.capture_method,
        "target": o.target,
        "collected_at": iso(o.collected_at),
        "thumb_url": file_url(o.thumb_path),
        "image_url": file_url(o.image_path),
        "width": o.image_width,
        "height": o.image_height,
    }
