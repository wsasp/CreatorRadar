"""Add accounts, discovery, searches and topics."""

import sqlalchemy as sa
from alembic import op

revision = "0002_discovery"
down_revision = "0001_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("native_id", sa.String, nullable=False),
        sa.Column("nickname", sa.Text, nullable=False),
        sa.Column("profile_url", sa.Text, nullable=False),
        sa.Column("collector_url", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("tracking", sa.String, nullable=False),
        sa.Column("lookback_days", sa.Integer),
        sa.Column("scan_limit", sa.Integer),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("last_job_id", sa.String(32), sa.ForeignKey("jobs.id")),
        sa.UniqueConstraint("platform", "native_id"),
    )
    op.create_index("ix_accounts_recent", "accounts", ["created_at", "id"])
    op.create_table(
        "works",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("platform", sa.String, nullable=False),
        sa.Column("native_id", sa.String, nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("author", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("original_url", sa.Text, nullable=False),
        sa.Column("collector_url", sa.Text, nullable=False),
        sa.Column("cover_url", sa.Text),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("published_at", sa.Integer),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("collected_at", sa.Integer, nullable=False),
        sa.Column("metrics", sa.JSON, nullable=False),
        sa.Column("reads", sa.JSON),
        sa.UniqueConstraint("platform", "native_id"),
    )
    op.create_index("ix_works_published", "works", ["published_at", "id"])
    op.create_table(
        "searches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("keyword", sa.Text, nullable=False),
        sa.Column("platforms", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_index("ix_searches_recent", "searches", ["created_at", "id"])
    op.create_table(
        "discoveries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(32), sa.ForeignKey("accounts.id")),
        sa.Column("search_id", sa.String(32), sa.ForeignKey("searches.id", ondelete="CASCADE")),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("rank", sa.Integer),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.UniqueConstraint("kind", "source_id", "work_id"),
    )
    op.create_index("ix_discoveries_work", "discoveries", ["work_id", "kind"])
    op.create_index("ix_discoveries_account", "discoveries", ["account_id", "work_id"])
    op.create_index("ix_discoveries_search", "discoveries", ["search_id", "rank"])
    op.create_table(
        "topics",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False, unique=True),
        sa.Column("source", sa.JSON, nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("archived", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
    )
    op.create_index("ix_topics_recent", "topics", ["archived", "created_at", "id"])
    op.create_table(
        "platform_sessions",
        sa.Column("platform", sa.String, primary_key=True),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("checked_at", sa.Integer, nullable=False),
    )
    op.create_table(
        "scan_dispatches",
        sa.Column("local_date", sa.String(10), primary_key=True),
        sa.Column("account_id", sa.String(32), sa.ForeignKey("accounts.id"), primary_key=True),
    )
    op.create_table(
        "capture_aliases",
        sa.Column("platform", sa.String, primary_key=True),
        sa.Column("link_key", sa.String, primary_key=True),
        sa.Column("native_id", sa.String, nullable=False),
    )


def downgrade():
    for table in (
        "capture_aliases",
        "scan_dispatches",
        "platform_sessions",
        "topics",
        "discoveries",
        "searches",
        "works",
        "accounts",
    ):
        op.drop_table(table)
