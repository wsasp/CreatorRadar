"""Create the workspace and persistent queue."""

import sqlalchemy as sa
from alembic import op

revision = "0001_workspace"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("jobs.id")),
        sa.Column("is_batch", sa.Boolean, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("platform", sa.String),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("phase", sa.String, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("started_at", sa.Integer),
        sa.Column("finished_at", sa.Integer),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String),
        sa.Column("lease_until", sa.Integer),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON),
        sa.Column("error_code", sa.String),
        sa.Column("message", sa.Text, nullable=False),
        sa.CheckConstraint("NOT is_batch OR parent_id IS NULL", name="batch_is_root"),
    )
    op.create_index(
        "uq_jobs_active_key", "jobs", ["kind", "dedupe_key"], unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_jobs_resource", "jobs", ["platform", "status"])
    op.create_index("ix_jobs_recent", "jobs", ["parent_id", "created_at", "id"])
    op.create_index("ix_jobs_claim", "jobs", ["status", "is_batch", "created_at", "id"])
    op.create_index("ix_jobs_lease", "jobs", ["status", "lease_until"])
    op.create_table(
        "job_attempts",
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column("number", sa.Integer, primary_key=True),
        sa.Column("worker_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("started_at", sa.Integer, nullable=False),
        sa.Column("finished_at", sa.Integer),
        sa.Column("error_code", sa.String),
        sa.Column("message", sa.Text),
    )
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("jobs.id")),
        sa.Column("attempt", sa.Integer),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("platform", sa.String),
        sa.Column("level", sa.String, nullable=False),
        sa.Column("code", sa.String, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_events_job", "job_events", ["job_id", "id"])
    op.create_index("ix_events_parent", "job_events", ["parent_id", "id"])
    op.create_index("ix_events_time", "job_events", ["created_at", "id"])
    op.create_table(
        "workers",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("heartbeat_at", sa.Integer, nullable=False),
    )
    setting_table = op.create_table(
        "settings", sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
    )
    op.bulk_insert(setting_table, [
        {"key": "enabled_platforms", "value": ["douyin", "xiaohongshu", "bilibili"]},
        {"key": "scan", "value": {
            "enabled": False, "time": "09:00", "lookback_days": 7, "limit": 20,
        }},
        {"key": "asr", "value": {"engine": "FunASR", "model": "paraformer-zh", "language": "zh"}},
        {"key": "ai", "value": {"base_url": "", "model": "", "key_configured": False}},
        {"key": "retention", "value": {"media_days": 3, "search_days": 14, "log_days": 14}},
    ])


def downgrade():
    for table in ("settings", "workers", "job_events", "job_attempts", "jobs"):
        op.drop_table(table)
