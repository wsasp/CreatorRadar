"""Add versioned topic processing and reusable media."""

import sqlalchemy as sa
from alembic import op

revision = "0003_processing"
down_revision = "0002_discovery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "transcriptions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("topic_id", sa.String(32), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("engine", sa.String, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("model_revision", sa.String),
        sa.Column("language", sa.String, nullable=False),
        sa.Column("audio_sha256", sa.String(64), nullable=False),
        sa.Column("segments", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_index("ix_transcriptions_topic", "transcriptions", ["topic_id", "created_at"])
    op.create_table(
        "analyses",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("topic_id", sa.String(32), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("transcription_id", sa.String(32), sa.ForeignKey("transcriptions.id")),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("prompt_version", sa.Integer, nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_snapshot", sa.JSON, nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_index("ix_analyses_topic", "analyses", ["topic_id", "created_at"])
    op.create_table(
        "media_cache",
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), primary_key=True),
        sa.Column("media_path", sa.Text),
        sa.Column("media_at", sa.Integer),
        sa.Column("audio_path", sa.Text),
        sa.Column("audio_at", sa.Integer),
    )
    # SQLite accepts inline foreign keys on nullable columns without rebuilding topics.
    for column, table in (
        ("transcription_id", "transcriptions"), ("analysis_id", "analyses"),
        ("asr_job_id", "jobs"), ("analysis_job_id", "jobs"),
    ):
        op.execute(f"ALTER TABLE topics ADD COLUMN {column} VARCHAR(32) REFERENCES {table}(id)")
    op.execute("""
        INSERT OR IGNORE INTO media_cache(work_id, media_path, media_at)
        SELECT w.id, json_extract(j.result, '$.media_path'), j.finished_at
        FROM jobs j JOIN works w ON w.id = json_extract(j.result, '$.work_id')
        WHERE j.kind = 'media_fetch' AND j.status = 'succeeded'
          AND json_type(j.result, '$.media_path') = 'text'
        ORDER BY j.finished_at DESC, j.id DESC
    """)


def downgrade():
    for column in ("analysis_job_id", "asr_job_id", "analysis_id", "transcription_id"):
        op.execute(f"ALTER TABLE topics DROP COLUMN {column}")
    for table in ("media_cache", "analyses", "transcriptions"):
        op.drop_table(table)
