from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Path, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.dialects.sqlite import insert

from . import analysis_client as ai
from . import codex_client, collector, covers, funasr_client, media, processing, queue
from . import discovery as d
from .db import write_transaction
from .schema import (
    PLATFORMS,
    accounts,
    capture_aliases,
    discoveries,
    jobs,
    platform_sessions,
    searches,
    settings,
    topics,
    works,
)

Platform = Literal[*PLATFORMS]
Id = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Cursor = Annotated[str | None, Query(pattern=r"^-?[0-9]{1,16}:[0-9a-f]{32}$")]
Limit = Annotated[int, Query(ge=1, le=100)]
Keyword = Annotated[str, Query(max_length=200)]
Link = Annotated[str, Field(min_length=1, max_length=4096)]
Days = Annotated[int, Field(ge=1, le=365)]
ScanLimit = Annotated[int, Field(ge=1, le=100)]


class AccountAdd(BaseModel, extra="forbid"):
    url: Link
    note: str = Field(default="", max_length=500)
    lookback_days: Days | None = None
    scan_limit: ScanLimit | None = None


class AccountChange(BaseModel, extra="forbid"):
    note: str | None = Field(default=None, max_length=500)
    tracking: Literal["active", "paused"] | None = None
    lookback_days: Days | None = None
    scan_limit: ScanLimit | None = None


class ScanRequest(BaseModel, extra="forbid"):
    account_ids: list[Id] | None = Field(default=None, min_length=1, max_length=100)
    platform: Platform | None = None
    q: str = Field(default="", max_length=200)


class SearchRequest(BaseModel, extra="forbid"):
    keyword: str = Field(min_length=1, max_length=200)
    platforms: list[Platform] = Field(min_length=1, max_length=3)


class CaptureRequest(BaseModel, extra="forbid"):
    url: Link


class AdoptRequest(BaseModel, extra="forbid"):
    work_id: Id
    kind: Literal["scan", "search"]
    source_id: Id


class ArchiveRequest(BaseModel, extra="forbid"):
    archived: bool


class ScanPlan(BaseModel, extra="forbid"):
    enabled: bool
    time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    lookback_days: Days
    limit: ScanLimit
    platforms: list[Platform] = Field(default_factory=list, max_length=3)


def validated_key(value: SecretStr) -> SecretStr:
    key = value.get_secret_value().strip()
    if not key or any(not 33 <= ord(character) <= 126 for character in key):
        raise ValueError("invalid key")
    return SecretStr(key)


class AsrSettings(BaseModel, extra="forbid", str_strip_whitespace=True):
    engine: Literal["FunASR", "faster-whisper"]
    model: str = Field(min_length=1, max_length=300)
    language: Literal["zh", "auto"]
    base_url: str = Field(default="", max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=1024, exclude=True)

    @field_validator("base_url")
    @classmethod
    def service_url(cls, value):
        normalized = ai.normalize_base(value)
        if not normalized:
            return ""
        parts = urlsplit(normalized)
        path = parts.path.removesuffix("/audio/transcriptions") or "/v1"
        return parts._replace(path=path).geturl()

    @field_validator("api_key")
    @classmethod
    def key_value(cls, value):
        return validated_key(value) if value is not None else None

    @model_validator(mode="after")
    def engine_configuration(self):
        if self.engine == "FunASR" and (self.language != "zh" or not self.base_url):
            raise ValueError("FunASR requires a service address and Chinese language")
        if self.engine == "faster-whisper" and (self.base_url or self.api_key is not None):
            raise ValueError("faster-whisper uses a local model")
        return self


class AiSettings(BaseModel, extra="forbid", str_strip_whitespace=True):
    provider: Literal["api", "codex_cli"] = "api"
    base_url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=200)
    codex_model: str = Field(default="", max_length=200)
    codex_reasoning_effort: str = Field(default="", pattern=r"^([a-z][a-z0-9_-]{0,31})?$")

    @field_validator("base_url")
    @classmethod
    def service_url(cls, value):
        return ai.normalize_base(value)

    @field_validator("codex_model")
    @classmethod
    def model_id(cls, value):
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("invalid Codex model ID")
        return value


class AiKey(BaseModel, extra="forbid"):
    api_key: SecretStr = Field(min_length=1, max_length=1024)
    base_url: str | None = Field(default=None, max_length=2048)

    @field_validator("api_key")
    @classmethod
    def key_value(cls, value):
        return validated_key(value)


class RetentionChange(BaseModel, extra="forbid"):
    media_days: Days
    log_days: Days


class SettingsChange(BaseModel, extra="forbid"):
    enabled_platforms: list[Platform] | None = Field(default=None, max_length=3)
    scan: ScanPlan | None = None
    asr: AsrSettings | None = None
    ai: AiSettings | None = None
    retention: RetentionChange | None = None


def matching(term, *columns):
    return or_(*(column.icontains(term.strip(), autoescape=True) for column in columns))


def paginate(connection, statement, order, identity, cursor, limit, *, ascending=False):
    pair = tuple_(order, identity)
    if cursor:
        value, item_id = cursor.split(":")
        position = (int(value), item_id)
        statement = statement.where(pair > position if ascending else pair < position)
    keys = (order.asc(), identity.asc()) if ascending else (order.desc(), identity.desc())
    rows = (
        connection.execute(
            statement.add_columns(
                order.label("_position"),
                identity.label("_identity"),
            )
            .order_by(*keys)
            .limit(limit + 1)
        )
        .mappings()
        .all()
    )
    items = rows[:limit]
    return items, f"{items[-1]['_position']}:{items[-1]['_identity']}" if len(
        rows
    ) > limit else None


def page(items, cursor, **extra):
    return {"items": items, "next_cursor": cursor, **extra}


def feed_source(account_id=None):
    source = select(discoveries.c.id).where(
        discoveries.c.work_id == works.c.id, discoveries.c.kind == "scan"
    )
    return (source.where(discoveries.c.account_id == account_id) if account_id else source).exists()


def recent_feed_publications():
    now = queue.now_ms()
    return works.c.published_at.between(now - d.DAY_MS, now)


def account_view(row, job_view):
    last_scan = None
    if row.get("scan_id"):
        last_scan = job_view(
            {
                "id": row["scan_id"],
                "status": row["scan_status"],
                "started_at": row["scan_started"],
                "finished_at": row["scan_finished"],
                "message": row["scan_message"],
                "result": row["scan_result"],
                "error_code": row["scan_error"],
            }
        )
    return {
        key: row[key]
        for key in (
            "id",
            "platform",
            "native_id",
            "nickname",
            "profile_url",
            "note",
            "tracking",
            "lookback_days",
            "scan_limit",
        )
    } | {"created_at": d.stamp(row["created_at"]), "last_scan": last_scan}


def account_query():
    return select(
        accounts,
        jobs.c.id.label("scan_id"),
        jobs.c.status.label("scan_status"),
        jobs.c.started_at.label("scan_started"),
        jobs.c.finished_at.label("scan_finished"),
        jobs.c.message.label("scan_message"),
        jobs.c.result.label("scan_result"),
        jobs.c.error_code.label("scan_error"),
    ).select_from(accounts.outerjoin(jobs, accounts.c.last_job_id == jobs.c.id))


def topic_query():
    return select(
        works,
        topics.c.id.label("topic_id"),
        topics.c.source,
        topics.c.snapshot,
        topics.c.archived,
        topics.c.created_at.label("adopted_at"),
    ).join(topics)


def topic_view(row, workspace, detail=False):
    result = {
        "id": row["topic_id"],
        "archived": row["archived"],
        "source": row["source"],
        "created_at": d.stamp(row["adopted_at"]),
        "work": d.work_view(
            row, topic_id=row["topic_id"], workspace=workspace, include_images=detail,
        ),
    }
    if detail:
        result["snapshot"] = row["snapshot"]
    return result


def search_view(row, job_view):
    return {
        "id": row["id"],
        "keyword": row["keyword"],
        "platforms": row["platforms"],
        "job_id": row["job_id"],
        "status": row["status"],
        "created_at": d.stamp(row["created_at"]),
    }


def overview(connection):
    counts = dict(
        connection.execute(
            select(accounts.c.platform, func.count())
            .where(accounts.c.tracking == "active")
            .group_by(accounts.c.platform)
        ).tuples().all()
    )
    feed_counts = dict(
        connection.execute(
            select(works.c.platform, func.count())
            .where(
                feed_source(),
            )
            .group_by(works.c.platform)
        ).tuples().all()
    )
    video = works.c.kind == "video"
    active, videos, transcribed, analyzed = connection.execute(
        select(
            func.count(),
            func.count().filter(video),
            func.count().filter(video, topics.c.transcription_id.is_not(None)),
            func.count(topics.c.analysis_id),
        ).select_from(topics.join(works, topics.c.work_id == works.c.id))
        .where(~topics.c.archived)
    ).one()
    return {
        "works_24h": connection.scalar(
            select(func.count())
            .select_from(works)
            .where(
                feed_source(),
                recent_feed_publications(),
            )
        ),
        "tracked_accounts": sum(counts.values()),
        "active_topics": active,
        "platforms": [
            {"platform": name, "accounts": counts.get(name, 0), "works": feed_counts.get(name, 0)}
            for name in PLATFORMS
        ],
        "transcription": {"completed": transcribed, "total": videos},
        "analysis": {"completed": analyzed, "total": active},
    }


def routes(engine, config, job_view):
    router = APIRouter(prefix="/api/v1")
    workspace = config.workspace_dir

    def work_with_cover(row, topic_id):
        return d.work_view(row, topic_id=topic_id, workspace=workspace)

    def work_image_response(path):
        return FileResponse(path, media_type="image/webp", headers={
            "X-Content-Type-Options": "nosniff",
        })

    @router.api_route("/works/{work_id}/cover", methods=["GET", "HEAD"])
    def work_cover(work_id: Id):
        with engine.connect() as connection:
            work = d.require_row(connection, works, work_id, "找不到该作品。")
        path = covers.cached(workspace, work["platform"], work["native_id"])
        if path is None:
            raise d.problem(404, "cover_not_found", "暂无本地封面。")
        return work_image_response(path)

    @router.api_route("/works/{work_id}/images/{index}", methods=["GET", "HEAD"])
    def work_image(work_id: Id, index: Annotated[int, Path(ge=0, lt=covers.MAX_ARTICLE_IMAGES)]):
        with engine.connect() as connection:
            work = d.require_row(connection, works, work_id, "找不到该作品。")
        images = work["image_urls"] or []
        if index < len(images):
            path = covers.cached(workspace, work["platform"], work["native_id"], images[index])
            if path is not None:
                return work_image_response(path)
        raise d.problem(404, "work_image_not_found", "暂无本地图片。")

    @router.get("/accounts")
    def list_accounts(
        platform: Platform | None = None, q: Keyword = "", cursor: Cursor = None, limit: Limit = 30
    ):
        statement = account_query().where(accounts.c.tracking != "removed")
        if platform:
            statement = statement.where(accounts.c.platform == platform)
        if q.strip():
            statement = statement.where(matching(q, accounts.c.nickname, accounts.c.note))
        with engine.connect() as connection:
            rows, next_cursor = paginate(
                connection, statement, accounts.c.created_at, accounts.c.id, cursor, limit
            )
        return page([account_view(row, job_view) for row in rows], next_cursor)

    @router.post("/accounts", status_code=202)
    def add_account(body: AccountAdd):
        link = d.input_link(body.url, {"account"})
        with write_transaction(engine) as connection:
            d.require_platform(d.read_settings(connection), link.platform)
            return {
                "job_id": queue.submit_in(
                    connection,
                    queue.Submission(
                        "account_resolve",
                        link.native_id,
                        "解析创作者主页",
                        link.platform,
                        {**body.model_dump(), "url": link.url, "trigger": "manual"},
                    ),
                )
            }

    @router.get("/accounts/{account_id}")
    def get_account(account_id: Id):
        with engine.connect() as connection:
            row = (
                connection.execute(account_query().where(accounts.c.id == account_id))
                .mappings()
                .first()
            )
            if row is None:
                raise d.problem(404, "account_not_found", "找不到该账号。")
            count = connection.scalar(
                select(func.count())
                .select_from(discoveries)
                .where(
                    discoveries.c.account_id == account_id,
                )
            )
            return {**account_view(row, job_view), "work_count": count}

    @router.patch("/accounts/{account_id}")
    def update_account(account_id: Id, body: AccountChange):
        values = body.model_dump(exclude_unset=True)
        for key in ("tracking", "note"):
            if key in values and values[key] is None:
                raise d.problem(422, "invalid_request", "跟踪状态和备注须为有效值。")
        with write_transaction(engine) as connection:
            row = d.require_row(connection, accounts, account_id, "找不到该账号。")
            if row["tracking"] == "removed":
                raise d.problem(409, "account_removed", "请重新添加主页以恢复已移除的账号。")
            if values:
                connection.execute(
                    accounts.update().where(accounts.c.id == account_id).values(**values)
                )
        return get_account(account_id)

    @router.delete("/accounts/{account_id}")
    def remove_account(account_id: Id):
        with write_transaction(engine) as connection:
            d.require_row(connection, accounts, account_id, "找不到该账号。")
            connection.execute(
                accounts.update().where(accounts.c.id == account_id).values(tracking="removed")
            )
        return {"id": account_id}

    @router.post("/account-scans", status_code=202)
    def scan_accounts(body: ScanRequest):
        with write_transaction(engine) as connection:
            values = d.read_settings(connection)
            statement = select(accounts).where(
                accounts.c.tracking == "active",
                accounts.c.platform.in_(values["enabled_platforms"]),
            )
            if body.account_ids:
                statement = statement.where(accounts.c.id.in_(body.account_ids))
            if body.platform:
                statement = statement.where(accounts.c.platform == body.platform)
            if body.q.strip():
                statement = statement.where(matching(body.q, accounts.c.nickname, accounts.c.note))
            rows = (
                connection.execute(statement.order_by(accounts.c.created_at, accounts.c.id))
                .mappings()
                .all()
            )
            if not rows:
                raise d.problem(409, "no_accounts", "当前范围没有可扫描的跟踪账号。")
            ids = [d.submit_scan(connection, row, "manual") for row in rows]
            return {"job_id": ids[0], "job_ids": ids}

    @router.get("/feed")
    def feed(
        platform: Platform | None = None,
        account_id: Id | None = None,
        q: Keyword = "",
        adopted: bool | None = None,
        since: Annotated[int | None, Query(ge=0)] = None,
        until: Annotated[int | None, Query(ge=0)] = None,
        recent: Literal["24h"] | None = None,
        cursor: Cursor = None,
        limit: Limit = 30,
    ):
        statement = (
            select(works, topics.c.id.label("topic_id"))
            .outerjoin(topics)
            .where(feed_source(account_id))
        )
        if platform:
            statement = statement.where(works.c.platform == platform)
        if q.strip():
            statement = statement.where(matching(q, works.c.title, works.c.body, works.c.author))
        if adopted is not None:
            statement = statement.where(
                topics.c.id.is_not(None) if adopted else topics.c.id.is_(None)
            )
        if recent == "24h":
            statement = statement.where(recent_feed_publications())
        if since is not None:
            statement = statement.where(works.c.published_at >= since)
        if until is not None:
            statement = statement.where(works.c.published_at <= until)
        if since is not None and until is not None and since > until:
            raise d.problem(422, "invalid_date_range", "开始日期须早于结束日期。")
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            count = connection.scalar(select(func.count()).select_from(statement.subquery()))
            rows, next_cursor = paginate(
                connection,
                statement,
                func.coalesce(works.c.published_at, -1),
                works.c.id,
                cursor,
                limit,
            )
        return page(
            [work_with_cover(row, row["topic_id"]) for row in rows], next_cursor, total=count
        )

    @router.get("/works/{work_id}")
    def get_work(work_id: Id):
        with engine.connect() as connection:
            work = d.require_row(connection, works, work_id, "找不到该作品。")
            topic_id = connection.scalar(select(topics.c.id).where(topics.c.work_id == work_id))
            sources = connection.execute(
                select(discoveries)
                .where(
                    discoveries.c.work_id == work_id,
                    or_(discoveries.c.search_id.is_(None), select(searches.c.id).where(
                        searches.c.id == discoveries.c.search_id, ~searches.c.hidden,
                    ).exists()),
                )
                .order_by(discoveries.c.created_at, discoveries.c.id)
            ).mappings()
            return {
                **d.work_view(work, topic_id=topic_id, workspace=workspace, include_images=True),
                "sources": [d.source_view(connection, row) for row in sources],
            }

    @router.get("/searches")
    def list_searches(cursor: Cursor = None, limit: Limit = 30):
        with engine.connect() as connection:
            rows, next_cursor = paginate(
                connection,
                select(searches, jobs.c.status).join(jobs).where(~searches.c.hidden),
                searches.c.created_at,
                searches.c.id,
                cursor,
                limit,
            )
        return page([search_view(row, job_view) for row in rows], next_cursor)

    def remove_search_history(search_id=None):
        statement = searches.update().where(~searches.c.hidden)
        if search_id is not None:
            statement = statement.where(searches.c.id == search_id)
        with write_transaction(engine) as connection:
            removed = connection.execute(statement.values(hidden=True)).rowcount
        return {"removed": removed}

    @router.delete("/searches")
    def clear_search_history():
        return remove_search_history()

    @router.delete("/searches/{search_id}")
    def delete_search_history(search_id: Id):
        return remove_search_history(search_id)

    @router.post("/searches", status_code=202)
    def start_search(body: SearchRequest):
        keyword = body.keyword.strip()
        if not keyword:
            raise d.problem(422, "empty_keyword", "请输入关键词。")
        platforms = list(dict.fromkeys(body.platforms))
        with write_transaction(engine) as connection:
            values = d.read_settings(connection)
            for platform in platforms:
                d.require_platform(values, platform)
            search_id = uuid4().hex
            params = {"search_id": search_id, "trigger": "manual"}
            job_id = queue.submit_in(
                connection,
                queue.Submission("search", search_id, keyword, parameters=params),
                tuple(
                    queue.Submission("search", search_id, keyword, name, params)
                    for name in platforms
                ),
            )
            connection.execute(
                searches.insert().values(
                    id=search_id,
                    job_id=job_id,
                    keyword=keyword,
                    platforms=platforms,
                    created_at=queue.now_ms(),
                )
            )
            return {"job_id": job_id, "search_id": search_id}

    @router.get("/searches/{search_id}/results")
    def search_results(
        search_id: Id,
        platform: Platform | None = None,
        sort: Literal["platform", "published"] = "platform",
        cursor: Cursor = None,
        limit: Limit = 30,
    ):
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            search = d.require_row(connection, searches, search_id, "搜索记录不存在或已到期。")
            if search["hidden"]:
                raise d.problem(404, "not_found", "搜索记录已删除。")
            root = queue.require_job(connection, search["job_id"])
            children = {
                row["platform"]: job_view(row) for row in queue.children_of(connection, root["id"])
            }
            statement = (
                select(works, topics.c.id.label("topic_id"))
                .join(
                    discoveries,
                    discoveries.c.work_id == works.c.id,
                )
                .outerjoin(topics, topics.c.work_id == works.c.id)
                .where(discoveries.c.search_id == search_id)
            )
            if platform:
                statement = statement.where(works.c.platform == platform)
            order = func.coalesce(works.c.published_at, -1)
            if sort == "platform":
                order = (
                    case(
                        {name: idx for idx, name in enumerate(search["platforms"])},
                        value=works.c.platform,
                        else_=3,
                    )
                    * 100000
                    + discoveries.c.rank
                )
            rows, next_cursor = paginate(
                connection,
                statement,
                order,
                works.c.id,
                cursor,
                limit,
                ascending=sort == "platform",
            )
        return page(
            [work_with_cover(row, row["topic_id"]) for row in rows],
            next_cursor,
            search=search_view({**search, "status": root["status"]}, job_view),
            children=[children[name] for name in search["platforms"]],
        )

    @router.post("/quick-captures", status_code=202)
    def quick_capture(body: CaptureRequest):
        link = d.input_link(body.url, {"work", "short"})
        with write_transaction(engine) as connection:
            d.require_platform(d.read_settings(connection), link.platform)
            alias = connection.scalar(
                select(capture_aliases.c.native_id).where(
                    capture_aliases.c.platform == link.platform,
                    capture_aliases.c.link_key == d.capture_key(link),
                )
            )
            key = alias or link.native_id or d.capture_key(link)
            return {
                "job_id": queue.submit_in(
                    connection,
                    queue.Submission(
                        "quick_capture",
                        key,
                        "采集单条作品",
                        link.platform,
                        {"url": link.url, "trigger": "manual"},
                    ),
                )
            }

    @router.get("/topics")
    def list_topics(
        platform: Platform | None = None,
        q: Keyword = "",
        archived: bool = False,
        cursor: Cursor = None,
        limit: Limit = 30,
    ):
        statement = topic_query().where(topics.c.archived == archived)
        if platform:
            statement = statement.where(works.c.platform == platform)
        if q.strip():
            statement = statement.where(matching(q, works.c.title, works.c.author, works.c.body))
        with engine.connect() as connection:
            rows, next_cursor = paginate(
                connection, statement, topics.c.created_at, topics.c.id, cursor, limit
            )
        return page([topic_view(row, workspace) for row in rows], next_cursor)

    @router.post("/topics")
    def adopt(body: AdoptRequest):
        with write_transaction(engine) as connection:
            work = d.require_row(connection, works, body.work_id, "找不到该作品。")
            source = (
                connection.execute(
                    select(discoveries).where(
                        discoveries.c.work_id == body.work_id,
                        discoveries.c.kind == body.kind,
                        discoveries.c.source_id == body.source_id,
                        or_(discoveries.c.search_id.is_(None), select(searches.c.id).where(
                            searches.c.id == discoveries.c.search_id, ~searches.c.hidden,
                        ).exists()),
                    )
                )
                .mappings()
                .first()
            )
            if source is None:
                raise d.problem(422, "invalid_source", "该作品没有对应的发现来源。")
            return {
                "topic_id": d.adopt(
                    connection, work, d.source_view(connection, source), queue.now_ms()
                )
            }

    @router.get("/topics/{topic_id}")
    def get_topic(topic_id: Id):
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            row = (
                connection.execute(topic_query().where(topics.c.id == topic_id)).mappings().first()
            )
            if row is None:
                raise d.problem(404, "topic_not_found", "找不到该选题。")
            topic = d.require_row(connection, topics, topic_id, "找不到该选题。")
            return {
                **topic_view(row, workspace, detail=True),
                **processing.topic_results(connection, topic, row, job_view),
            }

    @router.post("/topics/{topic_id}/transcriptions", status_code=202)
    def start_transcription(topic_id: Id):
        with write_transaction(engine) as connection:
            return {"job_id": processing.submit(connection, config, topic_id, "asr")}

    @router.post("/topics/{topic_id}/analyses", status_code=202)
    def start_analysis(topic_id: Id):
        with write_transaction(engine) as connection:
            return {"job_id": processing.submit(connection, config, topic_id, "analysis")}

    @router.patch("/topics/{topic_id}")
    def archive(topic_id: Id, body: ArchiveRequest):
        with write_transaction(engine) as connection:
            d.require_row(connection, topics, topic_id, "找不到该选题。")
            connection.execute(
                topics.update().where(topics.c.id == topic_id).values(archived=body.archived)
            )
        return get_topic(topic_id)

    @router.get("/scan-plan")
    def scan_plan():
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            values = d.read_settings(connection)
            count, next_run, last_job = d.scan_plan_status(connection, values)
        return {
            "eligible_accounts": count, "next_run_at": next_run,
            "last_job": job_view(last_job) if last_job else None,
        }

    @router.get("/settings")
    def get_settings():
        with engine.connect() as connection:
            values = d.read_settings(connection)
            states = {
                row["platform"]: {"state": row["state"], "checked_at": d.stamp(row["checked_at"])}
                for row in connection.execute(select(platform_sessions)).mappings()
            }
            login_jobs = {
                row["platform"]: row["id"]
                for row in connection.execute(
                    select(jobs).where(
                        jobs.c.kind == "platform_login",
                        jobs.c.status.in_(("queued", "running")),
                    )
                ).mappings()
            }
        ai_values = values["ai"]
        try:
            configured = bool(ai.read_key(config, ai_values["base_url"]))
            ai_state = "configured" if configured and ai_values["model"] else "unconfigured"
        except ai.ProcessingError:
            configured, ai_state = False, "unavailable"
        if ai_values["provider"] == "codex_cli":
            try:
                codex_client.executable()
                ai_state = "configured"
            except ai.ProcessingError:
                ai_state = "unavailable"
        asr_base = values["asr"].get("base_url", "")
        try:
            asr_configured = bool(asr_base and funasr_client.service_key(config, asr_base))
        except ai.ProcessingError:
            asr_configured = False
        return {
            **values,
            "asr": {**values["asr"], "base_url": asr_base, "key_configured": asr_configured},
            "ai": {**ai_values, "key_configured": configured},
            "workspace": config.workspace,
            "database_path": str(config.database_path),
            "media_path": str(media.root(config)),
            "integrations": {
                "crawler": collector.runtime_info(config),
                "asr": "installed"
                if config.bundled or config.asr_python.is_file() else "unavailable",
                "ai": ai_state,
            },
            "sessions": {
                name: {
                    **states.get(name, {"state": "unknown", "checked_at": None}),
                    "job_id": login_jobs.get(name),
                }
                for name in PLATFORMS
            },
        }

    @router.patch("/settings")
    def update_settings(body: SettingsChange):
        with write_transaction(engine) as connection:
            if body.asr and body.asr.engine == "FunASR":
                try:
                    if body.asr.api_key is not None:
                        funasr_client.save_key(
                            config, body.asr.base_url, body.asr.api_key.get_secret_value(),
                        )
                    else:
                        processing.funasr_key(config, body.asr.base_url)
                except ai.ProcessingError as exc:
                    code = str(exc)
                    raise d.problem(409, code, processing.MESSAGES[code]) from None
            for key, value in body.model_dump(exclude_none=True, exclude_unset=True).items():
                if key == "enabled_platforms":
                    value = list(dict.fromkeys(value))
                elif key in {"retention", "ai", "scan"}:
                    value = {**d.read_settings(connection)[key], **value}
                    if key == "scan" and "platforms" in value:
                        value["platforms"] = list(dict.fromkeys(value["platforms"]))
                statement = insert(settings).values(key=key, value=value)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[settings.c.key],
                        set_={"value": value},
                    )
                )
        return get_settings()

    def save_ai_key(value, expected_base=None):
        with write_transaction(engine) as connection:
            base = d.read_settings(connection)["ai"]["base_url"]
            if not base:
                raise d.problem(409, "ai_configuration_required", "请先保存 AI 服务地址。")
            if expected_base is not None and expected_base != base:
                raise d.problem(409, "ai_settings_changed", "AI 服务地址已更改，请刷新设置后重试。")
            try:
                ai.write_key(config, base, value)
            except ai.ProcessingError as exc:
                raise d.problem(503, str(exc), processing.MESSAGES[str(exc)]) from None
        return get_settings()

    @router.put("/settings/ai-key")
    def replace_ai_key(body: AiKey):
        return save_ai_key(body.api_key.get_secret_value(), body.base_url)

    @router.delete("/settings/ai-key")
    def remove_ai_key(base_url: Annotated[str | None, Query(max_length=2048)] = None):
        return save_ai_key(None, base_url)

    @router.post("/settings/asr/check")
    def check_asr():
        with engine.connect() as connection:
            values = d.read_settings(connection)["asr"]
        try:
            return processing.check_asr(config, values)
        except ai.ProcessingError as exc:
            code = str(exc)
            raise d.problem(409, code, processing.MESSAGES[code]) from None

    @router.get("/settings/ai/codex-models")
    async def codex_models():
        try:
            return {"models": await codex_client.models()}
        except ai.ProcessingError as exc:
            raise d.problem(503, str(exc), processing.MESSAGES[str(exc)]) from None

    @router.post("/settings/ai/test")
    async def test_ai():
        with engine.connect() as connection:
            values = d.read_settings(connection)["ai"]
        try:
            await processing.generate_analysis(config, processing.ai_selection(values), {
                "kind": "article",
                "sources": [{"id": 1, "text": "把素材按主题归档，再用关键词查找。"}],
            }, lambda: True)
        except ai.ProcessingError as exc:
            raise d.problem(409, str(exc), processing.MESSAGES[str(exc)]) from None
        return {"status": "ready"}

    @router.post("/platforms/{platform}/login", status_code=202)
    def login(platform: Platform):
        with write_transaction(engine) as connection:
            d.require_platform(d.read_settings(connection), platform)
            return {
                "job_id": queue.submit_in(
                    connection,
                    queue.Submission(
                        "platform_login",
                        platform,
                        "登录平台",
                        platform,
                        {"trigger": "manual"},
                    ),
                )
            }

    return router
