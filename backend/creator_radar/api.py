import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.params import Path
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError

from . import discovery_api, queue
from .config import Config
from .db import make_engine
from .schema import JOB_TYPES, LEVELS, PLATFORMS, STATUSES, attempts, events, jobs

Platform = Literal[*PLATFORMS]
JobType = Literal[*JOB_TYPES]
Status = Literal[*STATUSES]
Level = Literal[*LEVELS]
DateFilter = Annotated[AwareDatetime | None, Query()]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(pattern=r"^[0-9]{1,16}:[0-9a-f]{32}$")]
EventId = Annotated[int, Query(ge=0, le=2**63 - 1)]
ID_PATTERN = r"^[0-9a-f]{32}$"
Id = Annotated[str, Field(pattern=ID_PATTERN)]
JobPath = Annotated[str, Path(pattern=ID_PATTERN)]
WEB_ROUTES = ("", "feed", "topics", "accounts", "search", "jobs", "logs", "settings")


class RetryBody(BaseModel, extra="forbid"):
    child_ids: list[Id] | None = Field(default=None, min_length=1, max_length=100)


class ClearEventsBody(BaseModel, extra="forbid"):
    scope: Literal["completed", "all"]


def public_row(row):
    fields = ("dedupe_key", "parameters", "worker_id", "lease_until")
    data = {}
    for name, value in row.items():
        if name not in fields:
            data[name] = (
                datetime.fromtimestamp(value / 1000, UTC).isoformat()
                if name.endswith("_at") and value is not None else value
            )
    if "started_at" in row:
        end = row["finished_at"] or queue.now_ms()
        data["duration_ms"] = end - row["started_at"] if row["started_at"] is not None else None
    if "parameters" in row:
        params = row["parameters"]
        keys = ("trigger", "search_id", "account_id", "work_id", "topic_id")
        data["context"] = {key: params[key] for key in keys if key in params}
    return data


def error(status, code, message, job_id=None):
    return JSONResponse(
        status_code=status, content={"code": code, "message": message, "job_id": job_id},
    )


def date_conditions(column, since, until):
    if since and until and since > until:
        raise HTTPException(422, "开始时间必须早于或等于结束时间")
    if since:
        yield column >= int(since.timestamp() * 1000)
    if until:
        yield column <= int(until.timestamp() * 1000)


def event_query(level, platform, job_id, since, until):
    statement = select(events).where(*date_conditions(events.c.created_at, since, until))
    if level:
        statement = statement.where(events.c.level == level)
    if platform:
        statement = statement.where(events.c.platform == platform)
    if job_id:
        statement = statement.where(or_(events.c.job_id == job_id, events.c.parent_id == job_id))
    return statement


def create_app(config: Config | None = None):
    config = config or Config()
    engine = make_engine(config)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="CreatorRadar 2", lifespan=lifespan, version="0.2.0")
    origins = config.allowed_origins
    hosts = {"127.0.0.1", "localhost"} | {origin.split("//")[1] for origin in origins}

    @app.middleware("http")
    async def protect_local_workspace(request, call_next):
        if request.headers.get("host", "").lower() not in hosts:
            return error(400, "invalid_host", "请使用本机工作台地址")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin not in origins) or (
                origin is None and site not in (None, "same-origin", "none")
            ):
                return error(403, "invalid_origin", "请求来源不受信任")
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(queue.QueueError)
    async def queue_error(_request, exc):
        return error(exc.status, exc.code, str(exc), exc.job_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return error(422, "invalid_request", "请求参数格式有误")

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return error(exc.status_code, "invalid_request", str(exc.detail))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _exc):
        return error(503, "database_unavailable", "工作区数据库暂不可用")

    @app.get("/api/v1/health")
    def health():
        try:
            with engine.connect() as connection:
                last = queue.worker_heartbeat(connection)
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={
                "database": "unavailable", "worker": "offline", "worker_heartbeat_at": None,
            })
        return {
            "database": "ready", "worker": "online" if last and queue.now_ms() - last < 5000
            else "offline", **public_row({"worker_heartbeat_at": last}),
        }

    @app.get("/api/v1/overview")
    def overview():
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            root_jobs = select(jobs).where(jobs.c.parent_id.is_(None))
            recent = connection.execute(
                root_jobs.order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(5)
            ).mappings()
            running = connection.scalar(select(func.count()).select_from(jobs).where(
                jobs.c.parent_id.is_(None), jobs.c.status == "running",
            ))
            return {
                "recent_jobs": [public_row(row) for row in recent], "running_jobs": running,
                **discovery_api.overview(connection),
            }

    app.include_router(discovery_api.routes(engine, config, public_row))

    @app.get("/api/v1/jobs")
    def list_jobs(
        kind: JobType | None = None, status: Status | None = None,
        platform: Platform | None = None, since: DateFilter = None, until: DateFilter = None,
        cursor: Cursor = None, limit: Limit = 30,
    ):
        statement = select(jobs).where(
            jobs.c.parent_id.is_(None), *date_conditions(jobs.c.created_at, since, until),
        )
        if kind:
            statement = statement.where(jobs.c.kind == kind)
        if status:
            statement = statement.where(jobs.c.status == status)
        if platform:
            children = jobs.alias("children")
            has_platform = select(children.c.id).where(
                children.c.parent_id == jobs.c.id, children.c.platform == platform,
            ).exists()
            statement = statement.where(or_(jobs.c.platform == platform, has_platform))
        if cursor:
            at, job_id = cursor.split(":")
            statement = statement.where(tuple_(jobs.c.created_at, jobs.c.id) < (int(at), job_id))
        with engine.connect() as connection:
            rows = connection.execute(
                statement.order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit + 1)
            ).mappings().all()
        page = rows[:limit]
        next_cursor = f"{page[-1]['created_at']}:{page[-1]['id']}" if len(rows) > limit else None
        return {"items": [public_row(row) for row in page], "next_cursor": next_cursor}

    @app.get("/api/v1/jobs/{job_id}")
    def job_detail(job_id: JobPath):
        with engine.connect() as connection:
            # Explicit read transaction keeps the parent, children and attempts on one snapshot.
            connection.exec_driver_sql("BEGIN")
            job = queue.require_job(connection, job_id)
            history = connection.execute(select(attempts).where(
                attempts.c.job_id == job_id,
            ).order_by(attempts.c.number.desc())).mappings().all()
            return {
                **public_row(job), "children": [
                    public_row(row) for row in queue.children_of(connection, job_id)
                ], "attempts": [public_row(row) for row in history],
            }

    @app.post("/api/v1/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: JobPath):
        queue.cancel(engine, job_id)
        return {"job_id": job_id}

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: JobPath, body: Annotated[RetryBody | None, Body()] = None):
        queue.retry(engine, job_id, body.child_ids if body else None)
        return {"job_id": job_id}

    @app.post("/api/v1/system/self-test", status_code=202)
    def self_test():
        return {"job_id": queue.submit(engine, queue.Submission(
            "self_test", "system", "队列自检", parameters={"handler": "self_test"},
        ))}

    @app.post("/api/v1/events/clear")
    def clear_events(body: ClearEventsBody):
        return {"deleted_events": queue.clear_events(engine, body.scope)}

    @app.get("/api/v1/events")
    def list_events(
        level: Level | None = None, platform: Platform | None = None, job_id: Id | None = None,
        since: DateFilter = None, until: DateFilter = None, before: EventId | None = None,
        limit: Limit = 50,
    ):
        statement = event_query(level, platform, job_id, since, until)
        if before is not None:
            statement = statement.where(events.c.id < before)
        with engine.connect() as connection:
            rows = connection.execute(
                statement.order_by(events.c.id.desc()).limit(limit + 1)
            ).mappings().all()
        return {
            "items": [public_row(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/v1/events/stream")
    async def stream_events(
        request: Request, after: EventId | None = None,
        last_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        if last_id is not None and (
            not last_id.isascii() or not last_id.isdecimal()
            or len(last_id) > 19 or int(last_id) >= 2**63
        ):
            return error(422, "invalid_event_id", "事件序号格式有误")
        with engine.connect() as connection:
            latest = connection.scalar(select(func.max(events.c.id))) or 0
        cursor = int(last_id) if last_id is not None else (latest if after is None else after)

        def read_page(position):
            with engine.connect() as connection:
                return connection.execute(select(events).where(
                    events.c.id > position,
                ).order_by(events.c.id).limit(100)).mappings().all()

        async def generate():
            position = cursor
            yield "retry: 1000\n: connected\n\n"
            idle = 0
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(read_page, position)
                for row in rows:
                    position = row["id"]
                    payload = json.dumps(public_row(row), ensure_ascii=False)
                    yield f"id: {position}\nevent: job-event\ndata: {payload}\n\n"
                idle = idle + 1 if not rows else 0
                if idle >= 20:
                    yield ": keepalive\n\n"
                    idle = 0
                if len(rows) < 100:
                    await asyncio.sleep(0.5)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    def index():
        path = config.web_dist / "index.html"
        if not path.is_file():
            return error(503, "web_build_missing", "请先构建前端或使用 Vite 开发服务")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    assets = config.web_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    for route in WEB_ROUTES:
        app.add_api_route("/" + route, index, methods=["GET"], include_in_schema=False)

    return app
