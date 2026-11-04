"""Stage independent AI second opinions until human confirmation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0010_review_workspace'
down_revision = '0009_openrouter_only'
branch_labels = depends_on = None


def upgrade():
    op.create_table('review_batches',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('job_id', pg.UUID(as_uuid=True), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('model', sa.String(100), nullable=False),
        sa.Column('item_limit', sa.Integer(), nullable=False),
        sa.Column('concurrency', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('concurrency BETWEEN 1 AND 4'), sa.CheckConstraint('item_limit BETWEEN 1 AND 5000'))
    op.create_index('ix_review_batches_job_id', 'review_batches', ['job_id'])
    op.create_table('review_items',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True),
        sa.Column('batch_id', pg.UUID(as_uuid=True), sa.ForeignKey('review_batches.id', ondelete='CASCADE'), nullable=False),
        sa.Column('shot_id', pg.UUID(as_uuid=True), sa.ForeignKey('shots.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('source_labels_digest', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('token', sa.String(64)), sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('proposed_labels', pg.JSONB()), sa.Column('usage', pg.JSONB(), nullable=False),
        sa.Column('error', sa.Text()), sa.Column('decision', sa.String(30)),
        sa.Column('decided_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('batch_id', 'shot_id'))
    op.create_index('ix_review_items_shot_id', 'review_items', ['shot_id'])
    op.create_index('ix_review_items_batch_status', 'review_items', ['batch_id', 'status'])


def downgrade():
    op.drop_table('review_items')
    op.drop_table('review_batches')
