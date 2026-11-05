"""Allow up to twenty simultaneous review requests."""
from alembic import op

revision = '0011_review_concurrency'
down_revision = '0010_review_workspace'
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint('review_batches_concurrency_check', 'review_batches', type_='check')
    op.create_check_constraint('review_batches_concurrency_check', 'review_batches',
                               'concurrency BETWEEN 1 AND 20')


def downgrade():
    op.drop_constraint('review_batches_concurrency_check', 'review_batches', type_='check')
    op.create_check_constraint('review_batches_concurrency_check', 'review_batches',
                               'concurrency BETWEEN 1 AND 4')
