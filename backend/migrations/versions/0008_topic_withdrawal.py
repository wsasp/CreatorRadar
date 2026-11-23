"""Track cancelled topic collection while retaining processing history."""

import sqlalchemy as sa
from alembic import op

revision = "0008_topic_withdrawal"
down_revision = "0007_work_images"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("topics", sa.Column(
        "withdrawn", sa.Boolean, nullable=False, server_default=sa.false(),
    ))


def downgrade():
    op.drop_column("topics", "withdrawn")
