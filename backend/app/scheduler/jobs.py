"""Job lifecycle: create, start, pause, resume, cancel, limits, completion."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.events import emit
from app.models import CapturePlan, ConfigVersion, Job, ModelUsage, Observation, Project, Sample, Task
from app.schemas import JobCreate, ProjectConfig, effective_limits

ACTIVE_TASK_STATES = ("planned", "queued", "running", "captured", "manual_required")


class JobConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _now() -> datetime:
    return datetime.now(timezone.utc)


def snapshot_config(s: Session, project_id: uuid.UUID, kind: str, cfg: dict) -> ConfigVersion:
    payload = json.dumps(cfg, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    latest = s.execute(select(func.max(ConfigVersion.version)).where(ConfigVersion.project_id == project_id)).scalar() or 0
    cv = ConfigVersion(project_id=project_id, version=latest + 1, kind=kind, config=json.loads(payload), digest=digest)
    s.add(cv)
    s.flush()
    return cv


def validate_job_permissions(
    *,
    project: Project,
    target: str,
    method: str,
    worker_count: int,
    requests_per_minute: float,
    model_budget_usd: float,
    settings: Settings,
    max_captures: int | None = None,
) -> list[str]:
    cfg = ProjectConfig(**(project.config or {}))
    lim = effective_limits(cfg, settings)
    errors: list[str] = []
    if method not in lim.permitted_access_methods:
        errors.append(f"Access method '{method}' is not permitted for this project/operator")
    if target not in lim.permitted_targets:
        errors.append(f"Target '{target}' is not permitted for this project/operator")
    if max_captures is not None and max_captures > settings.max_captures_per_job:
        errors.append(f"max_captures {max_captures} exceeds the operator limit of {settings.max_captures_per_job} per job")
    if worker_count > lim.max_workers:
        errors.append(f"Worker count {worker_count} exceeds the limit of {lim.max_workers}")
    # Approval rate limits protect the live source; the local synthetic site has its own ceiling.
    rate_cap = lim.max_requests_per_minute if target != "synthetic" else 600.0
    if requests_per_minute > rate_cap:
        errors.append(f"Request rate {requests_per_minute}/min exceeds the limit of {rate_cap}/min for target '{target}'")
    if target == "google_maps" and lim.approval_expires_on and lim.approval_expires_on < date.today():
        errors.append(f"Research approval expired on {lim.approval_expires_on}")
    if target == "synthetic" and not settings.enable_mock_panorama:
        errors.append("Synthetic target requires ENABLE_MOCK_PANORAMA=true")
    if method == "openai_agent":
        if not (settings.openai_api_key and settings.openai_computer_use_model):
            errors.append("OpenAI agent requires OPENAI_API_KEY and OPENAI_COMPUTER_USE_MODEL")
        if model_budget_usd <= 0:
            errors.append("OpenAI agent requires a model budget > 0 USD")
        if target == "synthetic" and not settings.allow_synthetic_cloud_image_processing:
            errors.append(
                "OpenAI computer use needs screenshots; synthetic image egress is disabled (ALLOW_SYNTHETIC_CLOUD_IMAGE_PROCESSING=false)"
            )
        if target == "google_maps" and not lim.cloud_image_processing_allowed:
            errors.append("OpenAI computer use needs screenshots, but cloud image processing is not permitted: " + "; ".join(lim.reasons))
    if method == "streetview_static_api":
        if target != "google_maps":
            errors.append("Street View Static API method only applies to the google_maps target")
        if not settings.google_maps_api_key:
            errors.append("Street View Static API requires GOOGLE_MAPS_API_KEY")
    return errors


def create_job(s: Session, plan: CapturePlan, req: JobCreate, settings: Settings | None = None) -> Job:
    settings = settings or get_settings()
    if plan.status != "approved":
        raise JobConfigError(["Plan must be approved before a job can be created"])
    project = s.get(Project, plan.project_id)
    assert project is not None
    errors = validate_job_permissions(
        project=project,
        target=req.target,
        method=req.method,
        worker_count=req.worker_count,
        requests_per_minute=req.requests_per_minute,
        model_budget_usd=req.model_budget_usd,
        settings=settings,
        max_captures=req.max_captures,
    )
    if errors:
        raise JobConfigError(errors)

    params = plan.params or {}
    policy_keys = (
        "max_imagery_age_months",
        "unknown_date_policy",
        "max_camera_offset_m",
        "no_imagery_distance_m",
        "heading_tolerance_deg",
        "reuse_panorama_views",
    )
    job_settings = {
        "policy": {k: params.get(k) for k in policy_keys},
        "viewport": {"width": params.get("viewport_width", 1440), "height": params.get("viewport_height", 900)},
        "analysis_crop": req.analysis_crop,
        "mock": req.mock,
        "max_attempts": req.max_attempts,
    }
    cv = snapshot_config(
        s,
        project.id,
        "job",
        {
            "project_config": project.config,
            "plan_id": str(plan.id),
            "plan_params": params,
            "plan_config_version_id": str(plan.config_version_id) if plan.config_version_id else None,
            "job": req.model_dump(mode="json"),
            "operator": {
                "processing_mode": settings.processing_mode(),
                "openai_tool_type": settings.openai_computer_tool_type,
                "openai_prices": {
                    "input": settings.openai_price_input_per_mtok,
                    "cached_input": settings.openai_price_cached_input_per_mtok,
                    "output": settings.openai_price_output_per_mtok,
                },
                "lease_seconds": settings.lease_seconds,
            },
        },
    )
    job = Job(
        project_id=project.id,
        plan_id=plan.id,
        config_version_id=cv.id,
        name=req.name or f"{req.method} on {req.target}",
        status="draft",
        target=req.target,
        method=req.method,
        worker_count=req.worker_count,
        requests_per_minute=req.requests_per_minute,
        max_captures=req.max_captures,
        max_duration_s=int(req.max_duration_minutes * 60),
        model_budget_usd=req.model_budget_usd,
        settings=job_settings,
    )
    s.add(job)
    s.flush()
    samples = s.execute(
        select(Sample).where(Sample.plan_id == plan.id, Sample.excluded.is_(False)).order_by(Sample.ordinal)
    ).scalars().all()
    from app.models import Segment

    seg_keys = dict(s.execute(select(Segment.id, Segment.segment_key).where(Segment.plan_id == plan.id)).all())
    for smp in samples:
        s.add(
            Task(
                job_id=job.id,
                sample_id=smp.id,
                segment_key=seg_keys[smp.segment_id],
                ordinal=smp.ordinal,
                status="planned",
                max_attempts=req.max_attempts,
            )
        )
    emit(s, "job_created", f"Job created with {len(samples)} tasks ({req.method} / {req.target})", project_id=project.id, job_id=job.id)
    return job


def job_runtime_s(job: Job, now: datetime | None = None) -> float:
    rt = job.runtime_s or 0.0
    if job.status == "running" and job.running_since:
        rt += ((now or _now()) - job.running_since).total_seconds()
    return rt


def _accumulate(job: Job) -> None:
    if job.running_since:
        job.runtime_s = job_runtime_s(job)
        job.running_since = None


def accepted_capture_count(s: Session, job_id: uuid.UUID) -> int:
    return s.execute(
        select(func.count()).select_from(Observation).where(Observation.job_id == job_id, Observation.is_current.is_(True))
    ).scalar_one()


def project_capture_count(s: Session, project_id: uuid.UUID) -> int:
    return s.execute(
        select(func.count()).select_from(Observation).where(Observation.project_id == project_id, Observation.is_current.is_(True))
    ).scalar_one()


def model_spend_usd(s: Session, job_id: uuid.UUID) -> float:
    return float(s.execute(select(func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0.0)).where(ModelUsage.job_id == job_id)).scalar_one())


def limit_reason(s: Session, job: Job) -> str | None:
    if accepted_capture_count(s, job.id) >= job.max_captures:
        return f"Capture limit reached ({job.max_captures} captures)"
    project = s.get(Project, job.project_id)
    cfg = ProjectConfig(**(project.config or {}))
    if project_capture_count(s, job.project_id) >= cfg.max_captures_total:
        return f"Project capture limit reached ({cfg.max_captures_total})"
    if job_runtime_s(job) >= job.max_duration_s:
        return f"Duration limit reached ({job.max_duration_s // 60} min)"
    if job.method == "openai_agent" and model_spend_usd(s, job.id) >= job.model_budget_usd:
        return f"Estimated model budget reached (${job.model_budget_usd:.2f}, estimate)"
    if job.target == "google_maps" and cfg.approval_expires_on and cfg.approval_expires_on < date.today():
        return f"Research approval expired on {cfg.approval_expires_on}"
    return None


def start_job(s: Session, job: Job, settings: Settings | None = None) -> Job:
    settings = settings or get_settings()
    if job.status not in ("draft", "paused"):
        raise JobConfigError([f"Cannot start a job in status '{job.status}'"])
    project = s.get(Project, job.project_id)
    errors = validate_job_permissions(
        project=project,
        target=job.target,
        method=job.method,
        worker_count=job.worker_count,
        requests_per_minute=job.requests_per_minute,
        model_budget_usd=job.model_budget_usd,
        settings=settings,
    )
    reason = limit_reason(s, job)
    if reason:
        errors.append(reason)
    if errors:
        raise JobConfigError(errors)
    was = job.status
    s.execute(text("UPDATE tasks SET status = 'queued', next_attempt_at = now() WHERE job_id = :j AND status = 'planned'"), {"j": job.id})
    job.status = "running"
    job.running_since = _now()
    job.started_at = job.started_at or job.running_since
    job.pause_kind = None
    job.pause_reason = None
    emit(s, "job_resumed" if was == "paused" else "job_started", f"Job {'resumed' if was == 'paused' else 'started'}", project_id=job.project_id, job_id=job.id)
    return job


def resume_job(s: Session, job: Job, settings: Settings | None = None) -> Job:
    return start_job(s, job, settings)


def pause_job(s: Session, job: Job, kind: str, reason: str) -> Job:
    if job.status != "running":
        return job
    _accumulate(job)
    job.status = "paused"
    job.pause_kind = kind
    job.pause_reason = reason
    emit(s, "job_paused", f"Job paused ({kind}): {reason}", level="warning" if kind != "operator" else "info", project_id=job.project_id, job_id=job.id)
    return job


def cancel_job(s: Session, job: Job, reason: str = "cancelled by operator") -> Job:
    if job.status in ("cancelled", "completed"):
        return job
    _accumulate(job)
    job.status = "cancelled"
    job.pause_reason = reason
    job.finished_at = _now()
    s.execute(
        text("UPDATE tasks SET status = 'cancelled', finished_at = now(), outcome_reason = :r WHERE job_id = :j AND status IN ('planned','queued')"),
        {"j": job.id, "r": reason},
    )
    emit(s, "job_cancelled", f"Job cancelled: {reason}", project_id=job.project_id, job_id=job.id)
    return job


def task_counts(s: Session, job_id: uuid.UUID) -> dict[str, int]:
    rows = s.execute(select(Task.status, func.count()).where(Task.job_id == job_id).group_by(Task.status)).all()
    return {st: n for st, n in rows}


def maybe_complete_job(s: Session, job: Job) -> bool:
    if job.status != "running":
        return False
    counts = task_counts(s, job.id)
    if any(counts.get(st, 0) for st in ACTIVE_TASK_STATES):
        return False
    _accumulate(job)
    job.status = "completed"
    job.finished_at = _now()
    emit(s, "job_completed", f"Job completed: {counts}", project_id=job.project_id, job_id=job.id, data={"counts": counts})
    return True


def enforce_limits(s: Session, job: Job) -> str | None:
    if job.status != "running":
        return None
    reason = limit_reason(s, job)
    if reason:
        pause_job(s, job, "limit", reason)
    return reason
