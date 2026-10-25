"""Persistent visual capture runs, exclusive shot leases and assessment history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0008_capture_worker'
down_revision = '0007'
branch_labels = depends_on = None


def upgrade():
    op.create_table('capture_runs',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('job_id', pg.UUID(as_uuid=True), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('config', pg.JSONB(), nullable=False),
        sa.Column('requests', sa.Integer(), nullable=False), sa.Column('cost_usd', sa.Float(), nullable=False),
        sa.Column('finished', sa.Integer(), nullable=False), sa.Column('error', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_capture_runs_job_id', 'capture_runs', ['job_id'])
    op.create_table('capture_tasks',
        sa.Column('shot_id', pg.UUID(as_uuid=True), sa.ForeignKey('shots.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('run_id', pg.UUID(as_uuid=True), sa.ForeignKey('capture_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('worker_id', sa.String(100), nullable=False),
        sa.Column('token', sa.String(64), nullable=False), sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('steps', sa.Integer(), nullable=False),
        sa.Column('decision', pg.JSONB()), sa.Column('error', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_capture_tasks_run_id', 'capture_tasks', ['run_id'])
    op.create_table('capture_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_id', pg.UUID(as_uuid=True), sa.ForeignKey('capture_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('shot_id', pg.UUID(as_uuid=True), sa.ForeignKey('shots.id', ondelete='CASCADE')),
        sa.Column('data', pg.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_capture_events_run_id', 'capture_events', ['run_id'])


def downgrade():
    op.drop_table('capture_events')
    op.drop_table('capture_tasks')
    op.drop_table('capture_runs')
