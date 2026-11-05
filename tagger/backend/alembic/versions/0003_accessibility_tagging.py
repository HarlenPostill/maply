"""Persistent vision queue and host worker heartbeat.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tag_tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('shot_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('shots.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('model', sa.String(100), nullable=False),
        sa.Column('image_sha256', sa.String(64), nullable=False),
        sa.Column('worker_id', sa.String(100)),
        sa.Column('lease_token', sa.String(64)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text()),
        sa.Column('needs_review', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_tag_tasks_status', 'tag_tasks', ['status'])
    op.create_table('tag_workers',
        sa.Column('id', sa.String(100), primary_key=True),
        sa.Column('model', sa.String(100), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('error', sa.Text()),
    )


def downgrade():
    op.drop_table('tag_workers')
    op.drop_table('tag_tasks')
