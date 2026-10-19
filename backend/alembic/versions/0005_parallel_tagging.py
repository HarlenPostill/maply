"""Shared persisted limit for simultaneous vision sessions."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table('tag_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('parallel_sessions', sa.Integer(), nullable=False, server_default='1'),
        sa.CheckConstraint('id = 1'), sa.CheckConstraint('parallel_sessions BETWEEN 1 AND 8'))
    op.bulk_insert(table, [{'id': 1, 'parallel_sessions': 1}])


def downgrade():
    op.drop_table('tag_settings')
