"""Record the provider used for each annotation attempt."""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tag_tasks', sa.Column('provider', sa.String(30), nullable=False, server_default='antigravity'))


def downgrade():
    op.drop_column('tag_tasks', 'provider')
