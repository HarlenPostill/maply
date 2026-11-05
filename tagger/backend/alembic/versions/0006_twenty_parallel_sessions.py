"""Raise the persisted parallel session limit to twenty."""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

CONSTRAINT = 'tag_settings_parallel_sessions_check'


def upgrade():
    op.drop_constraint(CONSTRAINT, 'tag_settings', type_='check')
    op.create_check_constraint(CONSTRAINT, 'tag_settings', 'parallel_sessions BETWEEN 1 AND 20')


def downgrade():
    op.execute(sa.text('UPDATE tag_settings SET parallel_sessions = LEAST(parallel_sessions, 8)'))
    op.drop_constraint(CONSTRAINT, 'tag_settings', type_='check')
    op.create_check_constraint(CONSTRAINT, 'tag_settings', 'parallel_sessions BETWEEN 1 AND 8')
