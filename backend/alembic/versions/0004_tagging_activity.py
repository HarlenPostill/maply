"""Live annotation events, usage snapshots and cached confidence summaries."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tag_tasks', sa.Column('confidence', postgresql.JSONB()))
    op.add_column('tag_tasks', sa.Column('started_at', sa.DateTime(timezone=True)))
    op.add_column('tag_workers', sa.Column('usage', postgresql.JSONB()))
    op.create_table('tag_task_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tag_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False), sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('message', sa.String(500), nullable=False), sa.Column('data', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_tag_task_events_task_attempt', 'tag_task_events', ['task_id', 'attempt', 'id'])
    # Backfill existing valid labels once; keep repeated progress queries lightweight.
    import json
    connection = op.get_bind()
    for row in connection.execute(sa.text("""SELECT t.id, s.labels FROM tag_tasks t JOIN shots s ON s.id=t.shot_id
        WHERE t.status='completed' AND t.image_sha256=s.result->>'sha256'
        AND s.labels->'provenance'->>'image_sha256'=t.image_sha256""")).mappings():
        labels = row['labels']
        obs = [labels['footpath']] + [o for f in labels.get('features', []) for o in f['tags'].values()]
        scores = [o['confidence'] for o in obs if o.get('value') != 'unknown' and o.get('confidence') is not None]
        metrics = {'mean': sum(scores)/len(scores) if scores else None, 'minimum': min(scores) if scores else None,
                   'known': len(scores), 'unknown': len(obs)-len(scores)}
        connection.execute(sa.text('UPDATE tag_tasks SET confidence=CAST(:metrics AS jsonb) WHERE id=:id'),
                           {'metrics': json.dumps(metrics), 'id': row['id']})


def downgrade():
    op.drop_table('tag_task_events')
    op.drop_column('tag_workers', 'usage')
    op.drop_column('tag_tasks', 'started_at')
    op.drop_column('tag_tasks', 'confidence')
