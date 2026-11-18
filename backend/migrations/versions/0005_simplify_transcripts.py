"""Normalize previously saved Chinese transcripts to simplified Chinese."""

import sqlalchemy as sa
from alembic import op

from creator_radar.transcript_text import simplify_segments

revision = "0005_simplify_transcripts"
down_revision = "0004_asr_default"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    table = sa.table(
        "transcriptions", sa.column("id", sa.String), sa.column("language", sa.String),
        sa.column("segments", sa.JSON),
    )
    rows = connection.execute(
        sa.select(table).where(table.c.language.in_(("zh", "yue"))).order_by(table.c.id)
    ).mappings()
    for batch in rows.partitions(100):
        for row in batch:
            segments = simplify_segments(row["language"], row["segments"])
            if segments != row["segments"]:
                connection.execute(table.update().where(table.c.id == row["id"]).values(
                    segments=segments,
                ))


def downgrade():
    # Simplification can map more than one character to the same form.
    pass
