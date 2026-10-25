"""Route new analysis to OpenRouter and share capture uncertainty with manual review."""
from alembic import op
import sqlalchemy as sa

revision = '0009_openrouter_only'
down_revision = '0008_capture_worker'
branch_labels = depends_on = None


def upgrade():
    op.alter_column('tag_tasks', 'provider', server_default='openrouter')
    op.execute("""UPDATE tag_tasks SET provider='openrouter', status=CASE WHEN status='running' THEN 'queued' ELSE status END,
        lease_token=NULL, lease_until=NULL WHERE provider != 'openrouter' AND status IN ('queued','running')""")
    op.execute("""UPDATE shots SET flags=array_append(flags,'manual_review'),
        status=CASE WHEN status IN ('pending','retake') AND NOT (result ? 'file') THEN 'skipped' ELSE status END
        WHERE id IN (SELECT shot_id FROM capture_tasks WHERE status='review') AND NOT ('manual_review'=ANY(flags))""")


def downgrade():
    op.alter_column('tag_tasks', 'provider', server_default='antigravity')
