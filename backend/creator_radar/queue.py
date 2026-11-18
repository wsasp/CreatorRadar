from dataclasses import dataclass
from hashlib import sha256
from time import time_ns
from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, Engine, and_, exists, func, or_, select
from sqlalchemy.dialects.sqlite import insert

from .db import write_transaction
from .schema import (
    ACTIVE,
    PROCESSING_TYPES,
    RETRYABLE,
    attempts,
    events,
    jobs,
    searches,
    topics,
    workers,
)
from .security import redact, safe_result

LEASE_MS = 10_000


def now_ms() -> int:
    return time_ns() // 1_000_000


class QueueError(Exception):
    def __init__(self, status: int, code: str, message: str, job_id: str):
        super().__init__(message)
        self.status, self.code, self.job_id = status, code, job_id


@dataclass(frozen=True)
class Submission:
    kind: str
    key: str
    title: str
    platform: str | None = None
    parameters: dict[str, Any] | None = None


def append_event(connection, job, code, message, *, level="info", at=None):
    connection.execute(events.insert().values(
        job_id=job["id"], parent_id=job["parent_id"], attempt=job["attempt_count"] or None,
        created_at=at if at is not None else now_ms(), kind=job["kind"],
        platform=job["platform"], level=level, code=code, message=redact(message),
    ))


def _add(connection, job, *, parent_id=None, is_batch=False):
    scope = "batch" if is_batch else parent_id or "single"
    key = sha256(f"{scope}\0{job.platform or ''}\0{job.key}".encode()).hexdigest()
    job_id = uuid4().hex
    parameters = job.parameters or {}
    message = "定时扫描已排队" if parameters.get("trigger") == "scheduled" else "任务已排队"
    added = connection.execute(
        insert(jobs).values(
            id=job_id, parent_id=parent_id, is_batch=is_batch, kind=job.kind,
            platform=job.platform, title=redact(job.title), dedupe_key=key, status="queued",
            phase="queued", created_at=now_ms(), parameters=parameters, message=message,
        ).on_conflict_do_nothing(
            index_elements=[jobs.c.kind, jobs.c.dedupe_key], index_where=jobs.c.status.in_(ACTIVE),
        ).returning(*jobs.c)
    ).mappings().first()
    if added is None:
        existing = connection.execute(select(jobs.c.id).where(
            jobs.c.kind == job.kind, jobs.c.dedupe_key == key, jobs.c.status.in_(ACTIVE),
        )).scalar_one()
        return existing, False
    append_event(connection, added, "queued", message)
    return job_id, True


def submit(engine: Engine, job: Submission, children: tuple[Submission, ...] = ()) -> str:
    with write_transaction(engine) as connection:
        return submit_in(connection, job, children)


def submit_in(connection, job: Submission, children: tuple[Submission, ...] = ()) -> str:
    job_id, created = _add(connection, job, is_batch=bool(children))
    if created:
        for child in children:
            _add(connection, child, parent_id=job_id)
    return job_id


def _read(connection, job_id):
    return connection.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()


def require_job(connection, job_id):
    job = _read(connection, job_id)
    if job is None:
        raise QueueError(404, "job_not_found", "找不到该任务", job_id)
    return job


def _update(connection, job_id, **values):
    return connection.execute(
        jobs.update().where(jobs.c.id == job_id).values(**values).returning(*jobs.c)
    ).mappings().one()


def children_of(connection: Connection, job_id: str):
    return connection.execute(select(jobs).where(jobs.c.parent_id == job_id).order_by(
        jobs.c.created_at, jobs.c.id,
    )).mappings().all()


def clear_events(engine: Engine, scope: str) -> int:
    # Keep the full event history of an active batch until the batch is eligible.
    retained = ("running",) if scope == "all" else ACTIVE
    protected = select(jobs.c.id).where(jobs.c.status.in_(retained))
    with write_transaction(engine) as connection:
        return connection.execute(events.delete().where(
            events.c.job_id.not_in(protected),
            or_(events.c.parent_id.is_(None), events.c.parent_id.not_in(protected)),
        )).rowcount


def _summarize(connection, parent_id, at):
    if parent_id is None:
        return
    parent = require_job(connection, parent_id)
    children = children_of(connection, parent_id)
    counts = {status: sum(child["status"] == status for child in children)
              for status in {child["status"] for child in children}}
    if "running" in counts:
        status = "running"
    elif "queued" in counts:
        status = "queued"
    elif len(counts) == 1:
        status = next(iter(counts))
    elif "succeeded" in counts or "partially_succeeded" in counts:
        status = "partially_succeeded"
    else:
        status = next(state for state in ("failed", "interrupted", "waiting_login", "cancelled")
                      if state in counts)
    message = f"{counts.get('succeeded', 0)} / {len(children)} 个子任务成功"
    if partial := counts.get("partially_succeeded", 0):
        message += f"，{partial} 个有部分结果"
    updated = _update(
        connection, parent_id, status=status, phase=status, message=message, result=counts,
        started_at=parent["started_at"] or (
            at if any(child["started_at"] is not None for child in children) else None
        ),
        finished_at=None if status in ACTIVE else at,
        cancel_requested=parent["cancel_requested"] and status in ACTIVE,
    )
    if parent["status"] != status:
        append_event(connection, updated, status, message, at=at)


def claim(engine: Engine, worker_id: str):
    with write_transaction(engine) as connection:
        at = now_ms()
        active = jobs.alias("active")
        processing = jobs.c.kind.in_(PROCESSING_TYPES)
        active_processing = active.c.kind.in_(PROCESSING_TYPES)
        job = connection.execute(select(jobs).where(
            jobs.c.status == "queued", jobs.c.is_batch.is_(False),
            ~exists(select(active.c.id).where(active.c.status == "running", or_(
                and_(processing, active.c.kind == jobs.c.kind),
                and_(
                    ~processing, jobs.c.platform.is_not(None),
                    active.c.platform == jobs.c.platform,
                    or_(~active_processing, and_(
                        active.c.kind == "asr",
                        or_(
                            jobs.c.kind == "media_fetch",
                            active.c.phase.in_(("starting", "media")),
                        ),
                    )),
                ),
                and_(
                    jobs.c.kind == "asr", ~active_processing,
                    active.c.platform == jobs.c.platform,
                ),
            ))),
        ).order_by(jobs.c.created_at, jobs.c.id).limit(1)).mappings().first()
        if job is None:
            return None
        number = job["attempt_count"] + 1
        job = _update(
            connection, job["id"], status="running", phase="starting", started_at=at,
            attempt_count=number, worker_id=worker_id, lease_until=at + LEASE_MS,
            message="Worker 已认领任务",
        )
        connection.execute(attempts.insert().values(
            job_id=job["id"], number=number, worker_id=worker_id, status="running", started_at=at,
        ))
        append_event(connection, job, "started", job["message"], at=at)
        _summarize(connection, job["parent_id"], at)
        return job


def _owns(current, claimed, at):
    return (
        current is not None and current["status"] == "running"
        and current["worker_id"] == claimed["worker_id"]
        and current["attempt_count"] == claimed["attempt_count"]
        and current["lease_until"] > at
    )


def checkpoint(engine: Engine, claimed, phase: str | None = None, *, message=None) -> bool:
    with write_transaction(engine) as connection:
        at = now_ms()
        current = _read(connection, claimed["id"])
        if not _owns(current, claimed, at) or current["cancel_requested"]:
            return False
        values = {"lease_until": at + LEASE_MS}
        if phase is not None:
            values.update(phase=phase, message=redact(message) if message else "正在执行任务")
        job = _update(connection, current["id"], **values)
        if phase is not None:
            append_event(connection, job, phase, job["message"], at=at)
        return True


def save_checkpoint(engine: Engine, claimed, persist) -> bool:
    """Save a reusable intermediate artifact while this attempt still owns the job."""
    with write_transaction(engine) as connection:
        at = now_ms()
        current = _read(connection, claimed["id"])
        if not _owns(current, claimed, at) or current["cancel_requested"]:
            return False
        persist(connection, at)
        _update(connection, claimed["id"], lease_until=at + LEASE_MS)
        return True


def _finish(connection, job, status, message, at, *, code=None, result=None):
    was_processing = job["kind"] in PROCESSING_TYPES and job["status"] == "running"
    phase = job["phase"] if was_processing else status
    job = _update(
        connection, job["id"], status=status, phase=phase, finished_at=at,
        lease_until=None, worker_id=None, cancel_requested=False,
        result=safe_result(result), error_code=code, message=redact(message),
    )
    if job["attempt_count"] and job["started_at"] is not None:
        connection.execute(attempts.update().where(
            attempts.c.job_id == job["id"], attempts.c.number == job["attempt_count"],
            attempts.c.status == "running",
        ).values(status=status, finished_at=at, error_code=code, message=job["message"]))
    level = "error" if status == "failed" else (
        "warning" if status in ("waiting_login", "interrupted") else "info"
    )
    append_event(connection, job, code or status, job["message"], level=level, at=at)


def finish(engine: Engine, claimed, status: str, message: str, *, code=None, result=None,
           persist=None) -> bool:
    with write_transaction(engine) as connection:
        at = now_ms()
        current = _read(connection, claimed["id"])
        if not _owns(current, claimed, at):
            return False
        if current["cancel_requested"]:
            status, message, code, result = "cancelled", "任务已取消", None, None
        elif persist is not None:
            result = persist(connection, at)
        _finish(connection, current, status, message, at, code=code, result=result)
        _summarize(connection, current["parent_id"], at)
        return True


def cancel(engine: Engine, job_id: str) -> None:
    with write_transaction(engine) as connection:
        at = now_ms()
        job = require_job(connection, job_id)
        if job["status"] not in ACTIVE:
            raise QueueError(409, "job_not_active", "仅排队或运行中的任务可取消", job_id)
        targets = children_of(connection, job_id) if job["is_batch"] else [job]
        if job["is_batch"]:
            _update(connection, job_id, cancel_requested=True)
        for item in targets:
            if item["status"] == "queued":
                _finish(connection, item, "cancelled", "排队任务已取消", at)
            elif item["status"] == "running" and not item["cancel_requested"]:
                updated = _update(connection, item["id"], cancel_requested=True, message="正在取消")
                append_event(connection, updated, "cancel_requested", "已请求取消", at=at)
        _summarize(connection, job_id if job["is_batch"] else job["parent_id"], at)


def _require_dedupe_available(connection, job, request_id):
    if connection.execute(select(jobs.c.id).where(
        jobs.c.id != job["id"], jobs.c.kind == job["kind"],
        jobs.c.dedupe_key == job["dedupe_key"], jobs.c.status.in_(ACTIVE),
    )).first() is not None:
        raise QueueError(409, "job_duplicate", "已有相同任务在队列中", request_id)


def retry(engine: Engine, job_id: str, child_ids: list[str] | None) -> None:
    with write_transaction(engine) as connection:
        at = now_ms()
        job = require_job(connection, job_id)
        parent = require_job(connection, job["parent_id"]) if job["parent_id"] else job
        search_id = parent["parameters"].get("search_id")
        if parent["kind"] == "search" and search_id:
            visible_search = connection.scalar(select(searches.c.id).where(
                searches.c.id == search_id, ~searches.c.hidden,
            ))
            if visible_search is None:
                raise QueueError(409, "search_expired", "搜索记录已删除或到期，请重新搜索", job_id)
        if parent["cancel_requested"]:
            raise QueueError(409, "job_cancelling", "请等待取消完成后再重试", job_id)
        if job["is_batch"]:
            children = children_of(connection, job_id)
            targets = [item for item in children if item["status"] in RETRYABLE]
            if child_ids is not None:
                targets = [item for item in targets if item["id"] in child_ids]
                if {item["id"] for item in targets} != set(child_ids):
                    raise QueueError(
                        409, "invalid_retry_items", "指定子任务中含不可重试的项", job_id,
                    )
        else:
            if child_ids is not None:
                raise QueueError(409, "invalid_retry_items", "单任务不接受子任务选择", job_id)
            targets = [job] if job["status"] in RETRYABLE else []
        if not targets:
            raise QueueError(409, "job_not_retryable", "没有可重试的任务", job_id)
        _require_dedupe_available(connection, parent, job_id)
        for item in targets:
            _require_dedupe_available(connection, item, job_id)
            updated = _update(
                connection, item["id"], status="queued", phase="queued", started_at=None,
                finished_at=None, error_code=None, message="已重新排队", result=None,
            )
            topic_id = item["parameters"].get("topic_id")
            if item["kind"] in PROCESSING_TYPES and topic_id:
                column = topics.c[f"{item['kind']}_job_id"]
                connection.execute(
                    topics.update().where(topics.c.id == topic_id).values({column: item["id"]})
                )
            append_event(connection, updated, "retried", updated["message"], at=at)
        _summarize(connection, parent["id"] if parent["is_batch"] else None, at)


def recover(engine: Engine) -> None:
    with write_transaction(engine) as connection:
        at = now_ms()
        expired = connection.execute(select(jobs).where(
            jobs.c.status == "running", jobs.c.is_batch.is_(False), jobs.c.lease_until <= at,
        )).mappings().all()
        for job in expired:
            if job["cancel_requested"]:
                _finish(connection, job, "cancelled", "任务已取消", at)
            else:
                _finish(
                    connection, job, "interrupted", "Worker 租约已失效，可重试", at,
                    code="worker_lease_expired",
                )
        for parent_id in {job["parent_id"] for job in expired}:
            _summarize(connection, parent_id, at)


def heartbeat(engine: Engine, worker_id: str) -> None:
    with write_transaction(engine) as connection:
        at = now_ms()
        connection.execute(insert(workers).values(
            id=worker_id, heartbeat_at=at,
        ).on_conflict_do_update(index_elements=[workers.c.id], set_={"heartbeat_at": at}))
        connection.execute(workers.delete().where(workers.c.heartbeat_at < at - 60_000))


def worker_heartbeat(connection):
    return connection.scalar(select(func.max(workers.c.heartbeat_at)))
