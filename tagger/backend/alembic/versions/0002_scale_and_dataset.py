"""citywide jobs: dataset metadata columns and indexes

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("segments", sa.Column("osm", postgresql.JSONB(), nullable=False, server_default="{}"))
    op.add_column("shots", sa.Column("footpath", sa.String(20), nullable=False, server_default=""))
    op.add_column("shots", sa.Column("near_node_m", sa.Float(), nullable=True))
    op.add_column("shots", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shots", sa.Column("capture_source", sa.String(20), nullable=True))
    # Reserved for phase 2: accessibility tags from image analysis (one JSON object per photo).
    op.add_column("shots", sa.Column("labels", postgresql.JSONB(), nullable=True))
    op.alter_column("shots", "filename", type_=sa.String(200))
    op.create_index("ix_shots_job_status_ordinal", "shots", ["job_id", "status", "ordinal"])
    op.create_index("ix_shots_job_ordinal", "shots", ["job_id", "ordinal"])
    op.execute("CREATE INDEX ix_shots_geom ON shots USING gist (geom)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_shots_geom")
    op.drop_index("ix_shots_job_ordinal", "shots")
    op.drop_index("ix_shots_job_status_ordinal", "shots")
    for c in ("labels", "capture_source", "captured_at", "near_node_m", "footpath"):
        op.drop_column("shots", c)
    op.drop_column("segments", "osm")
