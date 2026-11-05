"""jobs, segments and shots

Revision ID: 0001
Revises:
"""
from __future__ import annotations

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("area", geoalchemy2.types.Geometry(srid=4326), nullable=True),
        sa.Column("streets", postgresql.JSONB(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "segments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("street", sa.String(200), nullable=False, server_default=""),
        sa.Column("geom", geoalchemy2.types.Geometry(geometry_type="LINESTRING", srid=4326), nullable=False),
        sa.Column("bearing_deg", sa.Float(), nullable=False),
        sa.Column("length_m", sa.Float(), nullable=False),
        sa.UniqueConstraint("job_id", "key"),
    )
    op.create_table(
        "shots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("batch", sa.Integer(), nullable=False),
        sa.Column("segment_key", sa.String(40), nullable=False),
        sa.Column("street", sa.String(200), nullable=False, server_default=""),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("view_kind", sa.String(30), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("geom", geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326), nullable=False),
        sa.Column("heading", sa.Float(), nullable=False),
        sa.Column("pitch", sa.Float(), nullable=False),
        sa.Column("fov", sa.Float(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("flags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("job_id", "code"),
    )


def downgrade() -> None:
    op.drop_table("shots")
    op.drop_table("segments")
    op.drop_table("jobs")
