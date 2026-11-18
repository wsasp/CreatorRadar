from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()
ACTIVE = ("queued", "running")
RETRYABLE = ("failed", "interrupted", "waiting_login", "partially_succeeded")
STATUSES = (*ACTIVE, *RETRYABLE, "succeeded", "cancelled")
JOB_TYPES = (
    "quick_capture", "account_resolve", "account_scan", "search", "platform_login",
    "media_fetch", "asr", "analysis", "self_test",
)
PROCESSING_TYPES = ("asr", "analysis")
PLATFORMS = ("douyin", "xiaohongshu", "bilibili")
LEVELS = ("info", "warning", "error")

jobs = Table(
    "jobs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("parent_id", ForeignKey("jobs.id")),
    Column("is_batch", Boolean, nullable=False),
    Column("kind", String, nullable=False),
    Column("platform", String),
    Column("title", String, nullable=False),
    Column("dedupe_key", String(64), nullable=False),
    Column("status", String, nullable=False),
    Column("phase", String, nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("started_at", Integer),
    Column("finished_at", Integer),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("worker_id", String),
    Column("lease_until", Integer),
    Column("cancel_requested", Boolean, nullable=False, server_default="0"),
    Column("parameters", JSON, nullable=False),
    Column("result", JSON),
    Column("error_code", String),
    Column("message", Text, nullable=False),
    CheckConstraint("NOT is_batch OR parent_id IS NULL", name="batch_is_root"),
)
Index(
    "uq_jobs_active_key", jobs.c.kind, jobs.c.dedupe_key, unique=True,
    sqlite_where=jobs.c.status.in_(ACTIVE),
)
Index("ix_jobs_resource", jobs.c.platform, jobs.c.status)
Index("ix_jobs_recent", jobs.c.parent_id, jobs.c.created_at, jobs.c.id)
Index("ix_jobs_claim", jobs.c.status, jobs.c.is_batch, jobs.c.created_at, jobs.c.id)
Index("ix_jobs_lease", jobs.c.status, jobs.c.lease_until)

attempts = Table(
    "job_attempts",
    metadata,
    Column("job_id", ForeignKey("jobs.id"), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("worker_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("started_at", Integer, nullable=False),
    Column("finished_at", Integer),
    Column("error_code", String),
    Column("message", Text),
)

events = Table(
    "job_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("parent_id", ForeignKey("jobs.id")),
    Column("attempt", Integer),
    Column("created_at", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("platform", String),
    Column("level", String, nullable=False),
    Column("code", String, nullable=False),
    Column("message", Text, nullable=False),
    sqlite_autoincrement=True,
)
Index("ix_events_job", events.c.job_id, events.c.id)
Index("ix_events_parent", events.c.parent_id, events.c.id)
Index("ix_events_time", events.c.created_at, events.c.id)

workers = Table(
    "workers",
    metadata,
    Column("id", String, primary_key=True),
    Column("heartbeat_at", Integer, nullable=False),
)
settings = Table(
    "settings", metadata, Column("key", String, primary_key=True),
    Column("value", JSON, nullable=False),
)

accounts = Table(
    "accounts", metadata,
    Column("id", String(32), primary_key=True),
    Column("platform", String, nullable=False),
    Column("native_id", String, nullable=False),
    Column("nickname", Text, nullable=False),
    Column("profile_url", Text, nullable=False),
    Column("collector_url", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("tracking", String, nullable=False),
    Column("lookback_days", Integer),
    Column("scan_limit", Integer),
    Column("created_at", Integer, nullable=False),
    Column("last_job_id", ForeignKey("jobs.id")),
    UniqueConstraint("platform", "native_id"),
)
Index("ix_accounts_recent", accounts.c.created_at, accounts.c.id)

works = Table(
    "works", metadata,
    Column("id", String(32), primary_key=True),
    Column("platform", String, nullable=False),
    Column("native_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("author", Text),
    Column("body", Text),
    Column("original_url", Text, nullable=False),
    Column("collector_url", Text, nullable=False),
    Column("cover_url", Text),
    Column("image_urls", JSON),
    Column("duration_seconds", Integer),
    Column("published_at", Integer),
    Column("created_at", Integer, nullable=False),
    Column("collected_at", Integer, nullable=False),
    Column("metrics", JSON, nullable=False),
    Column("reads", JSON),
    UniqueConstraint("platform", "native_id"),
)
Index("ix_works_published", works.c.published_at, works.c.id)

searches = Table(
    "searches", metadata,
    Column("id", String(32), primary_key=True),
    Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True),
    Column("keyword", Text, nullable=False),
    Column("platforms", JSON, nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("hidden", Boolean, nullable=False, server_default="0"),
)
Index("ix_searches_recent", searches.c.created_at, searches.c.id)

discoveries = Table(
    "discoveries", metadata,
    Column("id", String(32), primary_key=True),
    Column("work_id", ForeignKey("works.id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("source_id", String(32), nullable=False),
    Column("account_id", ForeignKey("accounts.id")),
    Column("search_id", ForeignKey("searches.id", ondelete="CASCADE")),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("rank", Integer),
    Column("created_at", Integer, nullable=False),
    UniqueConstraint("kind", "source_id", "work_id"),
)
Index("ix_discoveries_work", discoveries.c.work_id, discoveries.c.kind)
Index("ix_discoveries_account", discoveries.c.account_id, discoveries.c.work_id)
Index("ix_discoveries_search", discoveries.c.search_id, discoveries.c.rank)

topics = Table(
    "topics", metadata,
    Column("id", String(32), primary_key=True),
    Column("work_id", ForeignKey("works.id"), nullable=False, unique=True),
    Column("source", JSON, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("archived", Boolean, nullable=False),
    Column("created_at", Integer, nullable=False),
    Column("transcription_id", ForeignKey("transcriptions.id")),
    Column("analysis_id", ForeignKey("analyses.id")),
    Column("asr_job_id", ForeignKey("jobs.id")),
    Column("analysis_job_id", ForeignKey("jobs.id")),
)
Index("ix_topics_recent", topics.c.archived, topics.c.created_at, topics.c.id)

transcriptions = Table(
    "transcriptions", metadata,
    Column("id", String(32), primary_key=True),
    Column("topic_id", ForeignKey("topics.id"), nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True),
    Column("engine", String, nullable=False),
    Column("model", String, nullable=False),
    Column("model_revision", String),
    Column("language", String, nullable=False),
    Column("audio_sha256", String(64), nullable=False),
    Column("segments", JSON, nullable=False),
    Column("created_at", Integer, nullable=False),
)
Index("ix_transcriptions_topic", transcriptions.c.topic_id, transcriptions.c.created_at)

analyses = Table(
    "analyses", metadata,
    Column("id", String(32), primary_key=True),
    Column("topic_id", ForeignKey("topics.id"), nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False, unique=True),
    Column("transcription_id", ForeignKey("transcriptions.id")),
    Column("model", String, nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("input_fingerprint", String(64), nullable=False),
    Column("input_snapshot", JSON, nullable=False),
    Column("content", JSON, nullable=False),
    Column("created_at", Integer, nullable=False),
)
Index("ix_analyses_topic", analyses.c.topic_id, analyses.c.created_at)

media_cache = Table(
    "media_cache", metadata,
    Column("work_id", ForeignKey("works.id"), primary_key=True),
    Column("media_path", Text),
    Column("media_at", Integer),
    Column("audio_path", Text),
    Column("audio_at", Integer),
)

platform_sessions = Table(
    "platform_sessions", metadata,
    Column("platform", String, primary_key=True),
    Column("state", String, nullable=False),
    Column("checked_at", Integer, nullable=False),
)
scan_dispatches = Table(
    "scan_dispatches", metadata,
    Column("local_date", String(10), primary_key=True),
    Column("account_id", ForeignKey("accounts.id"), primary_key=True),
)
capture_aliases = Table(
    "capture_aliases", metadata,
    Column("platform", String, primary_key=True),
    Column("link_key", String, primary_key=True),
    Column("native_id", String, nullable=False),
)
