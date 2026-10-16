"""Transactional task completion and verification finalisation.

The worker writes image files atomically *before* calling `complete_task`.
`complete_task` then, inside one transaction:

1. row-locks the task and checks the caller still holds the current lease
   token (stale workers are rejected; their files become orphans);
2. re-verifies every artefact on disk (checksum, format, dimensions) and the
   required metadata -- an agent's claim of success is never sufficient;
3. inserts observations (partial unique index => one current observation
   per planned view per job) and association links;
4. moves the task to `captured` / `no_imagery` and ends the attempt.

`finalize_task` (idempotent) then derives `verified` / `needs_review`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.capture.verification import CaptureEvaluation, PageState, PlannedViewInfo
from app.events import emit
from app.models import Crop, Observation, ObservationLink, Panorama, PlannedView, Task
from app.scheduler.queue import Claim, _clear_worker, _end_attempt, lock_owned_task
from app.storage.files import verify_artifact


class ArtifactInvalid(RuntimeError):
    pass


@dataclass
class CropRecord:
    crop_id: uuid.UUID
    x: int
    y: int
    width: int
    height: int
    purpose: str
    rel_path: str
    sha256: str


@dataclass
class CaptureRecord:
    observation_id: uuid.UUID
    view: PlannedViewInfo
    image_rel: str
    image_format: str
    width: int
    height: int
    nbytes: int
    sha256: str
    thumb_rel: str | None
    state: PageState
    evaluation: CaptureEvaluation
    collected_at: datetime
    source_url: str
    capture_method: str
    device_scale_factor: float = 1.0
    crop: CropRecord | None = None

    @property
    def rel_paths(self) -> list[str]:
        out = [self.image_rel]
        if self.thumb_rel:
            out.append(self.thumb_rel)
        if self.crop:
            out.append(self.crop.rel_path)
        return out


@dataclass
class ReuseRecord:
    view_id: str
    observation_id: uuid.UUID
    camera_to_sample_m: float | None
    heading_delta_deg: float | None
    association_status: str


@dataclass
class TaskOutcome:
    kind: str = "captured"  # captured | no_imagery
    captures: list[CaptureRecord] = field(default_factory=list)
    reuses: list[ReuseRecord] = field(default_factory=list)
    missing_views: list[dict] = field(default_factory=list)
    no_imagery_reason: str | None = None
    panorama: dict | None = None
    evidence: dict = field(default_factory=dict)
    model_calls: int = 0
    actions: int = 0

    def all_rel_paths(self) -> list[str]:
        return [p for c in self.captures for p in c.rel_paths]


@dataclass
class CompletionResult:
    accepted: bool
    reason: str
    status: str | None = None


REQUIRED = ("image_rel", "sha256", "image_format", "width", "height", "collected_at", "capture_method")


def _validate_metadata(c: CaptureRecord) -> list[str]:
    problems = [f"missing {k}" for k in REQUIRED if getattr(c, k) in (None, "")]
    if c.evaluation.verification_status not in ("verified", "needs_review"):
        problems.append(f"capture status {c.evaluation.verification_status} cannot be stored as current")
    if c.view.viewport_w <= 0 or c.view.viewport_h <= 0:
        problems.append("invalid viewport")
    return problems


def _point(lng, lat):
    if lng is None or lat is None:
        return None
    return from_shape(Point(float(lng), float(lat)), srid=4326)


def _upsert_panorama(s: Session, source: str, st: PageState) -> uuid.UUID | None:
    if not st.pano_id:
        return None
    pid = s.execute(
        text(
            """
            INSERT INTO panoramas (id, source, pano_id, geom, position_provenance, imagery_date, imagery_date_precision,
                                   imagery_date_provenance, source_metadata, first_seen_at, last_seen_at)
            VALUES (:id, :src, :pano, CASE WHEN :lng IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) END,
                    :pp, :d, :dp, :dprov, '{}'::jsonb, now(), now())
            ON CONFLICT (source, pano_id) DO UPDATE SET last_seen_at = now()
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "src": source,
            "pano": st.pano_id,
            "lng": st.camera_lng,
            "lat": st.camera_lat,
            "pp": st.camera_provenance,
            "d": st.imagery_date,
            "dp": st.imagery_date_precision,
            "dprov": st.imagery_date_provenance,
        },
    ).scalar_one()
    return pid


def complete_task(
    s: Session,
    *,
    claim: Claim,
    worker_id: str,
    outcome: TaskOutcome,
    job,
    sample,
    segment,
) -> CompletionResult:
    row = lock_owned_task(s, claim.task_id, claim.lease_token, ("running",))
    if row is None:
        emit(
            s,
            "stale_completion_rejected",
            f"Rejected completion from {worker_id}: lease token no longer current",
            level="warning",
            job_id=claim.job_id,
            task_id=claim.task_id,
            worker_id=worker_id,
        )
        s.execute(
            text("UPDATE task_attempts SET outcome = 'stale_rejected', ended_at = now() WHERE task_id = :t AND lease_token = :tok"),
            {"t": claim.task_id, "tok": claim.lease_token},
        )
        return CompletionResult(False, "stale lease")

    # Supervisor-side verification of artefacts and metadata.
    for c in outcome.captures:
        problems = _validate_metadata(c)
        problems += verify_artifact(c.image_rel, c.sha256, c.width, c.height, c.image_format)
        if problems:
            raise ArtifactInvalid(f"view {c.view.id}: " + "; ".join(problems))

    source = "synthetic" if job.target == "synthetic" else ("static_api" if job.method == "streetview_static_api" else "google_maps")
    for c in outcome.captures:
        view_uuid = uuid.UUID(c.view.id)
        # A direct capture supersedes any earlier current observation / reuse link for the view.
        s.execute(
            update(Observation)
            .where(Observation.job_id == job.id, Observation.planned_view_id == view_uuid, Observation.is_current.is_(True))
            .values(is_current=False, verification_status="superseded")
        )
        s.execute(
            update(ObservationLink)
            .where(ObservationLink.job_id == job.id, ObservationLink.planned_view_id == view_uuid, ObservationLink.active.is_(True))
            .values(active=False)
        )
        st, ev = c.state, c.evaluation
        pano_ref = _upsert_panorama(s, source, st)
        obs = Observation(
            id=c.observation_id,
            project_id=job.project_id,
            job_id=job.id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            worker_id=worker_id,
            plan_id=job.plan_id,
            segment_id=segment.id,
            segment_key=segment.segment_key,
            sample_id=sample.id,
            planned_view_id=view_uuid,
            intended_side=c.view.side,
            view_kind=c.view.view_kind,
            requested_geom=sample.requested_geom,
            camera_geom=_point(st.camera_lng, st.camera_lat),
            camera_position_provenance=st.camera_provenance,
            camera_offset_m=ev.camera_offset_m,
            panorama_ref=pano_ref,
            pano_id=st.pano_id,
            pano_id_provenance=st.pano_id_provenance,
            source_url=c.source_url,
            planned_heading_deg=c.view.heading,
            planned_pitch_deg=c.view.pitch,
            planned_fov_deg=c.view.fov,
            heading_deg=st.heading,
            heading_provenance=st.heading_provenance,
            pitch_deg=st.pitch,
            pitch_provenance=st.pitch_provenance,
            fov_deg=st.fov,
            fov_provenance=st.fov_provenance,
            viewport_w=c.view.viewport_w,
            viewport_h=c.view.viewport_h,
            device_scale_factor=c.device_scale_factor,
            imagery_date=st.imagery_date,
            imagery_date_precision=st.imagery_date_precision,
            imagery_date_provenance=st.imagery_date_provenance,
            collected_at=c.collected_at,
            image_path=c.image_rel,
            image_format=c.image_format,
            image_width=c.width,
            image_height=c.height,
            image_bytes=c.nbytes,
            sha256=c.sha256,
            thumb_path=c.thumb_rel,
            capture_method=c.capture_method,
            target=job.target,
            config_version_id=job.config_version_id,
            attribution=st.attribution,
            verification_status=ev.verification_status,
            association_status=ev.association_status,
            quality_flags=ev.flags,
            verification_evidence=ev.evidence,
            is_current=True,
            idempotency_key=f"{claim.task_id}:{c.view.id}:{claim.lease_token}",
        )
        s.add(obs)
        s.flush()  # surfaces unique-index violations inside this transaction
        s.add(
            ObservationLink(
                job_id=job.id,
                observation_id=obs.id,
                planned_view_id=view_uuid,
                sample_id=sample.id,
                link_kind="direct",
                association_status=ev.association_status,
                camera_to_sample_m=ev.camera_offset_m,
                heading_delta_deg=ev.heading_delta_deg,
            )
        )
        if c.crop:
            s.add(
                Crop(
                    id=c.crop.crop_id,
                    observation_id=obs.id,
                    raw_sha256=c.sha256,
                    x=c.crop.x,
                    y=c.crop.y,
                    width=c.crop.width,
                    height=c.crop.height,
                    purpose=c.crop.purpose,
                    path=c.crop.rel_path,
                    sha256=c.crop.sha256,
                )
            )

    for r in outcome.reuses:
        view_uuid = uuid.UUID(r.view_id)
        exists = s.execute(
            select(ObservationLink.id).where(
                ObservationLink.job_id == job.id, ObservationLink.planned_view_id == view_uuid, ObservationLink.active.is_(True)
            )
        ).first()
        if exists:
            continue
        s.add(
            ObservationLink(
                job_id=job.id,
                observation_id=r.observation_id,
                planned_view_id=view_uuid,
                sample_id=sample.id,
                link_kind="reused_same_panorama",
                association_status=r.association_status,
                camera_to_sample_m=r.camera_to_sample_m,
                heading_delta_deg=r.heading_delta_deg,
            )
        )

    new_status = "no_imagery" if outcome.kind == "no_imagery" else "captured"
    result = {
        "captures": len(outcome.captures),
        "reused_views": len(outcome.reuses),
        "missing_views": outcome.missing_views,
        "panorama": outcome.panorama,
        "no_imagery_reason": outcome.no_imagery_reason,
    }
    s.execute(
        update(Task)
        .where(Task.id == claim.task_id)
        .values(
            status=new_status,
            result=result,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            finished_at=text("now()"),
            outcome_reason=outcome.no_imagery_reason if new_status == "no_imagery" else None,
            view_filter=None,
        )
    )
    _end_attempt(s, claim.task_id, claim.lease_token, new_status, None, outcome.evidence, outcome.model_calls, outcome.actions)
    _clear_worker(s, worker_id)
    emit(
        s,
        "task_" + new_status,
        f"Task {claim.ordinal}: {len(outcome.captures)} captured, {len(outcome.reuses)} reused, {len(outcome.missing_views)} missing"
        + (f" — no imagery: {outcome.no_imagery_reason}" if new_status == "no_imagery" else ""),
        job_id=job.id,
        task_id=claim.task_id,
        worker_id=worker_id,
    )
    return CompletionResult(True, "ok", new_status)


def finalize_task(s: Session, task_id: uuid.UUID, statuses: tuple[str, ...] = ("captured",)) -> str | None:
    """Derive verified / needs_review for a captured task. Idempotent.

    Reviews call this with statuses=("verified", "needs_review") to re-derive
    the task state after an operator decision.
    """
    task = s.execute(select(Task).where(Task.id == task_id).with_for_update()).scalar_one_or_none()
    if task is None or task.status not in statuses:
        return task.status if task else None
    views = s.execute(select(PlannedView.id).where(PlannedView.sample_id == task.sample_id)).scalars().all()
    reasons: list[str] = []
    for vid in views:
        obs = s.execute(
            select(Observation.verification_status, Observation.association_status).where(
                Observation.job_id == task.job_id, Observation.planned_view_id == vid, Observation.is_current.is_(True)
            )
        ).first()
        if obs:
            if obs.verification_status != "verified":
                reasons.append(f"view {str(vid)[:8]} {obs.verification_status}")
            continue
        link = s.execute(
            select(ObservationLink.link_kind).where(
                ObservationLink.job_id == task.job_id, ObservationLink.planned_view_id == vid, ObservationLink.active.is_(True)
            )
        ).first()
        if link is None:
            reasons.append(f"view {str(vid)[:8]} missing")
    task.status = "needs_review" if reasons else "verified"
    task.outcome_reason = "; ".join(reasons)[:2000] if reasons else None
    return task.status


def finalize_captured_tasks(s: Session) -> int:
    ids = s.execute(select(Task.id).where(Task.status == "captured")).scalars().all()
    for tid in ids:
        finalize_task(s, tid)
    return len(ids)
