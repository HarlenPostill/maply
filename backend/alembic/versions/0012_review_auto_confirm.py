"""Remember which review batches authorize automatic acceptance."""
from alembic import op
import sqlalchemy as sa

revision = '0012_review_auto_confirm'
down_revision = '0011_review_concurrency'
branch_labels = depends_on = None


def upgrade():
    op.add_column('review_batches', sa.Column('auto_confirm', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('review_batches', 'auto_confirm')
