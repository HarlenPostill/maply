"""Three tables: a job, the street segments it covers, and its shots.

A shot is one planned Street View screenshot: where to stand, which way to
look, the link that opens exactly that view, and the file Codex must save.
After "Check results" it also records what Codex actually saved and logged.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)  # folder name
    area = mapped_column(Geometry("GEOMETRY", srid=4326, spatial_index=False))  # blocked-out area, if any
    streets: Mapped[dict] = mapped_column(JSONB, nullable=False)  # FeatureCollection of the lines planned
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("job_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(40), nullable=False)  # stable, geometry-derived
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    street: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    geom = mapped_column(Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=False)
    bearing_deg: Mapped[float] = mapped_column(Float, nullable=False)  # stored direction, true north
    length_m: Mapped[float] = mapped_column(Float, nullable=False)
    osm: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # highway class, way ids, sidewalk/kerb/surface tags


SHOT_STATUSES = ("pending", "captured", "needs_check", "no_imagery", "blocked", "skipped", "accepted", "retake")


class Shot(Base):
    __tablename__ = "shots"
    __table_args__ = (
        UniqueConstraint("job_id", "code"),
        Index("ix_shots_job_status_ordinal", "job_id", "status", "ordinal"),
        Index("ix_shots_job_ordinal", "job_id", "ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)  # e.g. S007-L1
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    batch: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_key: Mapped[str] = mapped_column(String(40), nullable=False)
    street: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # street | corner
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # left | right | none, relative to segment direction
    view_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    # Planned camera: requested position on the street and the view to take.
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    geom = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    heading: Mapped[float] = mapped_column(Float, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, nullable=False)
    fov: Mapped[float] = mapped_column(Float, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)  # relative to the job's images/ folder
    footpath: Mapped[str] = mapped_column(String(20), default="", nullable=False)  # compass side, e.g. "west"
    near_node_m: Mapped[float | None] = mapped_column(Float)  # distance to the nearest intersection/corner
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_source: Mapped[str | None] = mapped_column(String(20))  # extension | codex
    labels: Mapped[dict | None] = mapped_column(JSONB)  # reserved for phase 2 (accessibility tags from image analysis)
    # After capture / checking results.
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    flags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tag_task: Mapped[TagTask | None] = relationship("TagTask", uselist=False, lazy="selectin", viewonly=True)


class TagTask(Base):
    __tablename__ = "tag_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="antigravity", server_default="antigravity")
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    needs_review: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TagWorker(Base):
    __tablename__ = "tag_workers"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSONB)


class TagTaskEvent(Base):
    __tablename__ = "tag_task_events"
    __table_args__ = (Index("ix_tag_task_events_task_attempt", "task_id", "attempt", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tag_tasks.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TagSettings(Base):
    __tablename__ = 'tag_settings'
    __table_args__ = (CheckConstraint('id = 1'), CheckConstraint('parallel_sessions BETWEEN 1 AND 20'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    parallel_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
