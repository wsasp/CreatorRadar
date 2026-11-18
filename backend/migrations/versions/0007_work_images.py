"""Store article images in their original order."""

import sqlalchemy as sa
from alembic import op

revision = "0007_work_images"
down_revision = "0006_search_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("works", sa.Column("image_urls", sa.JSON))


def downgrade():
    op.drop_column("works", "image_urls")
