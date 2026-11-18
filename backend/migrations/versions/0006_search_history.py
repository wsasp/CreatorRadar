"""Hide removed search history while in-flight searches finish."""

import sqlalchemy as sa
from alembic import op

revision = "0006_search_history"
down_revision = "0005_simplify_transcripts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("searches", sa.Column("hidden", sa.Boolean, nullable=False, server_default="0"))


def downgrade():
    op.drop_column("searches", "hidden")
