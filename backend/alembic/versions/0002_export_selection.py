"""record export selection policy

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("exports", sa.Column("include", sa.String(30), nullable=False, server_default="verified_and_review"))
    op.add_column("exports", sa.Column("copy_mode", sa.String(10), nullable=False, server_default="hardlink"))


def downgrade() -> None:
    op.drop_column("exports", "copy_mode")
    op.drop_column("exports", "include")
