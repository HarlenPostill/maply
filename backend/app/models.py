"""Database schema.

Design notes
------------
* Requested sample position, actual camera position, intended footpath,
  view orientation and the image<->footpath association are stored as
  *separate* fields with their own provenance. Camera coordinates are never
  presented as the location of anything visible in the image.
* Segment direction is the stored start->end order of `segments.geom`.
  "left"/"right" always mean left/right of that stored direction, regardless
  of which way a worker travels.
* Task leases use a fencing token (`lease_token`). A completion is only
  accepted if it presents the current token while the task is still running.
* At most one *current* observation exists per (job, planned view), enforced
  by a partial unique index, so retries and crashed workers cannot produce
  duplicate accepted captures.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------
# Project & configuration
# --------------------------------------------------------------------------


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Geographic scope (approval boundary). Collection outside it is refused.
    scope_geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ConfigVersion(Base):
    """Immutable snapshot of the configuration a plan or job ran under."""

    __tablename__ = "config_versions"
    __table_args__ = (UniqueConstraint("project_id", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # plan | job
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()


class Route(Base):
    """Operator-supplied geometry: drawn route, polygon, GeoJSON import or Street View seed."""

    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # route|polygon|import|streetview_seed
    geom = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=False)
    properties: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------
# Capture plan
# --------------------------------------------------------------------------


class CapturePlan(Base):
    __tablename__ = "capture_plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)  # draft|approved|superseded
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    route_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    config_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("config_versions.id"))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("plan_id", "segment_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_plans.id", ondelete="CASCADE"), index=True)
    route_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("routes.id", ondelete="SET NULL"))
    # Stable, geometry-derived ID: identical geometry in the same stored
    # direction always yields the same key across re-plans.
    segment_key: Mapped[str] = mapped_column(String(40), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    geom = mapped_column(Geometry("LINESTRING", srid=4326), nullable=False)
    length_m: Mapped[float] = mapped_column(Float, nullable=False)
    # Geodesic azimuth (true north, degrees) from first to last vertex.
    bearing_deg: Mapped[float] = mapped_column(Float, nullable=False)
    # Approximate footpath centrelines offset from the drawn line. These are
    # planning aids, not surveyed footpath geometry.
    footpath_left_geom = mapped_column(Geometry("LINESTRING", srid=4326))
    footpath_right_geom = mapped_column(Geometry("LINESTRING", srid=4326))
    sides: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class PlanNode(Base):
    """Intersections / crossing approaches, recorded separately from street samples."""

    __tablename__ = "plan_nodes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_plans.id", ondelete="CASCADE"), index=True)
    node_key: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # turn_vertex|route_crossing|route_endpoint|user_marked
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    segment_keys: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    detection: Mapped[str] = mapped_column(String(40), default="heuristic_candidate", nullable=False)


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_plans.id", ondelete="CASCADE"), index=True)
    segment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plan_nodes.id", ondelete="SET NULL"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    seq_in_segment: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), default="street", nullable=False)  # street|crossing_approach|seed
    chainage_m: Mapped[float] = mapped_column(Float, nullable=False)
    # Requested position on the route (NOT a camera position).
    requested_geom = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    original_geom = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    local_bearing_deg: Mapped[float] = mapped_column(Float, nullable=False)
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PlannedView(Base):
    __tablename__ = "planned_views"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_plans.id", ondelete="CASCADE"), index=True)
    sample_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("samples.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # left|right|none
    view_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    heading_deg: Mapped[float] = mapped_column(Float, nullable=False)
    pitch_deg: Mapped[float] = mapped_column(Float, nullable=False)
    fov_deg: Mapped[float] = mapped_column(Float, nullable=False)
    viewport_w: Mapped[int] = mapped_column(Integer, nullable=False)
    viewport_h: Mapped[int] = mapped_column(Integer, nullable=False)
    # Orientation is a *proposal* derived from local bearing; not proof the footpath is visible.
    orientation_basis: Mapped[str] = mapped_column(String(60), default="local_route_bearing", nullable=False)


# --------------------------------------------------------------------------
# Jobs, tasks, workers
# --------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_plans.id", ondelete="CASCADE"), index=True)
    config_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("config_versions.id"))
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # draft|running|paused|completed|cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    target: Mapped[str] = mapped_column(String(30), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    worker_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    requests_per_minute: Mapped[float] = mapped_column(Float, default=6.0, nullable=False)
    max_captures: Mapped[int] = mapped_column(Integer, nullable=False)
    max_duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    model_budget_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    runtime_s: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_kind: Mapped[str | None] = mapped_column(String(30))  # operator|limit|manual_intervention|error
    pause_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


TASK_STATES = (
    "planned",
    "queued",
    "running",
    "captured",
    "verified",
    "needs_review",
    "no_imagery",
    "manual_required",
    "failed",
    "cancelled",
)
TERMINAL_TASK_STATES = ("verified", "needs_review", "no_imagery", "failed", "cancelled")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("job_id", "sample_id"),
        Index("ix_tasks_claim", "job_id", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    sample_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("samples.id", ondelete="CASCADE"))
    segment_key: Mapped[str] = mapped_column(String(40), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="planned", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    # Restrict a recapture to specific planned views (null = all views of the sample).
    view_filter: Mapped[list | None] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_no"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(80), nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = _created()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running|captured|no_imagery|failed|interrupted|lease_expired|stale_rejected|manual_required
    outcome: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    host: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer)
    # starting|idle|running|awaiting_operator|stalled|stopped
    status: Mapped[str] = mapped_column(String(30), default="starting", nullable=False)
    method: Mapped[str | None] = mapped_column(String(40))
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    message: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = _created()
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_screenshot_path: Mapped[str | None] = mapped_column(Text)
    latest_screenshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    browser_profile_dir: Mapped[str | None] = mapped_column(Text)


class WorkerCommand(Base):
    """Durable operator -> worker command queue (manual mode and interventions)."""

    __tablename__ = "worker_commands"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------


class Panorama(Base):
    """Source panorama metadata as observed. Source IDs may change over time;
    observations reference this row and keep their own copies of the values."""

    __tablename__ = "panoramas"
    __table_args__ = (UniqueConstraint("source", "pano_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    pano_id: Mapped[str] = mapped_column(String(200), nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326))
    position_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    imagery_date: Mapped[str | None] = mapped_column(String(20))
    imagery_date_precision: Mapped[str] = mapped_column(String(10), default="unknown", nullable=False)
    imagery_date_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    source_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index(
            "uq_observation_current_per_view",
            "job_id",
            "planned_view_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_observations_job_status", "job_id", "verification_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_attempts.id", ondelete="CASCADE"))
    worker_id: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_plans.id", ondelete="CASCADE"))
    segment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("segments.id", ondelete="CASCADE"))
    segment_key: Mapped[str] = mapped_column(String(40), nullable=False)
    sample_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("samples.id", ondelete="CASCADE"))
    planned_view_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planned_views.id", ondelete="CASCADE"))
    intended_side: Mapped[str] = mapped_column(String(10), nullable=False)
    view_kind: Mapped[str] = mapped_column(String(30), nullable=False)

    # 1. requested sample position
    requested_geom = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    # 2. actual camera position as reported by the source (never "the kerb")
    camera_geom = mapped_column(Geometry("POINT", srid=4326))
    camera_position_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    camera_offset_m: Mapped[float | None] = mapped_column(Float)

    panorama_ref: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("panoramas.id", ondelete="SET NULL"))
    pano_id: Mapped[str | None] = mapped_column(String(200))
    pano_id_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)

    # 4. orientation: planned vs observed, each with provenance
    planned_heading_deg: Mapped[float] = mapped_column(Float, nullable=False)
    planned_pitch_deg: Mapped[float] = mapped_column(Float, nullable=False)
    planned_fov_deg: Mapped[float] = mapped_column(Float, nullable=False)
    heading_deg: Mapped[float | None] = mapped_column(Float)
    heading_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    pitch_deg: Mapped[float | None] = mapped_column(Float)
    pitch_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    fov_deg: Mapped[float | None] = mapped_column(Float)
    fov_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    viewport_w: Mapped[int] = mapped_column(Integer, nullable=False)
    viewport_h: Mapped[int] = mapped_column(Integer, nullable=False)
    device_scale_factor: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # imagery capture date (from the source) vs collection time (ours)
    imagery_date: Mapped[str | None] = mapped_column(String(20))
    imagery_date_precision: Mapped[str] = mapped_column(String(10), default="unknown", nullable=False)
    imagery_date_provenance: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # local artefact
    image_path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to DATA_DIR
    image_format: Mapped[str] = mapped_column(String(10), nullable=False)
    image_width: Mapped[int] = mapped_column(Integer, nullable=False)
    image_height: Mapped[int] = mapped_column(Integer, nullable=False)
    image_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    thumb_path: Mapped[str | None] = mapped_column(Text)

    capture_method: Mapped[str] = mapped_column(String(40), nullable=False)
    target: Mapped[str] = mapped_column(String(30), nullable=False)
    config_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("config_versions.id"))
    attribution: Mapped[str | None] = mapped_column(Text)

    # 5. verification and association
    # pending|verified|needs_review|rejected|superseded
    verification_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # verified|uncertain|unresolved  (image <-> intended footpath)
    association_status: Mapped[str] = mapped_column(String(20), default="unresolved", nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    verification_evidence: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="none", nullable=False)  # none|accepted|rejected
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class ObservationLink(Base):
    """Associates an observation with planned views it may cover.

    Every current observation has one `direct` link to its own planned view.
    When several requested samples resolve to the same panorama, extra
    `reused_same_panorama` links are recorded instead of re-capturing; these are
    `uncertain` unless the camera is close to that sample.
    """

    __tablename__ = "observation_links"
    __table_args__ = (
        Index(
            "uq_link_active_per_view",
            "job_id",
            "planned_view_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id", ondelete="CASCADE"), index=True)
    planned_view_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planned_views.id", ondelete="CASCADE"))
    sample_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("samples.id", ondelete="CASCADE"))
    link_kind: Mapped[str] = mapped_column(String(30), nullable=False)  # direct|reused_same_panorama
    association_status: Mapped[str] = mapped_column(String(20), nullable=False)
    camera_to_sample_m: Mapped[float | None] = mapped_column(Float)
    heading_delta_deg: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class Crop(Base):
    """Optional analysis crop. Always references the raw image and crop geometry."""

    __tablename__ = "crops"

    id: Mapped[uuid.UUID] = _uuid_pk()
    observation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("observations.id", ondelete="CASCADE"), index=True)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(60), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------
# Accounting, egress and audit
# --------------------------------------------------------------------------


class ModelUsage(Base):
    __tablename__ = "model_usage"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_attempts.id", ondelete="SET NULL"))
    worker_id: Mapped[str | None] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    response_id: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    images_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_source: Mapped[str] = mapped_column(String(30), default="provider_reported", nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    price_basis: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


class EgressEvent(Base):
    """Every attempted request to an external model provider. Never stores payloads."""

    __tablename__ = "egress_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _created()
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(80))
    data_class: Mapped[str] = mapped_column(String(20), nullable=False)  # none|synthetic|research
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # allowed|blocked
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_job_created", "job_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _created()
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(80))
    level: Mapped[str] = mapped_column(String(10), default="info", nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class SystemCheck(Base):
    """Startup / capability checks (e.g. provider model access)."""

    __tablename__ = "system_checks"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # ok|failed|skipped|unconfigured
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Export(Base):
    __tablename__ = "exports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    path: Mapped[str] = mapped_column(Text, nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    include: Mapped[str] = mapped_column(String(30), default="verified_and_review", nullable=False)
    copy_mode: Mapped[str] = mapped_column(String(10), default="hardlink", nullable=False)
    created_at: Mapped[datetime] = _created()


__all__ = [
    "Project",
    "ConfigVersion",
    "Route",
    "CapturePlan",
    "Segment",
    "PlanNode",
    "Sample",
    "PlannedView",
    "Job",
    "Task",
    "TaskAttempt",
    "Worker",
    "WorkerCommand",
    "Panorama",
    "Observation",
    "ObservationLink",
    "Crop",
    "ModelUsage",
    "EgressEvent",
    "Event",
    "Export",
    "SystemCheck",
    "TASK_STATES",
    "TERMINAL_TASK_STATES",
    "date",
]
