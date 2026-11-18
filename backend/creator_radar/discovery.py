from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert

from . import collector, covers, media, queue
from .db import write_transaction
from .links import LinkError, parse_link
from .schema import (
    ACTIVE,
    accounts,
    capture_aliases,
    discoveries,
    jobs,
    platform_sessions,
    scan_dispatches,
    searches,
    settings,
    topics,
    works,
)

DAY_MS = 86_400_000
METRICS = ("plays", "likes", "comments", "favorites", "shares")
OPERATIONS = {
    "account_resolve": "account",
    "account_scan": "scan",
    "quick_capture": "detail",
    "search": "search",
    "platform_login": "login",
    "media_fetch": "media",
}
ERRORS = {
    "runtime_unavailable": "采集环境未就绪，请重启工作台自动安装并查看启动日志。",
    "runtime_revision_mismatch": "MediaCrawler 版本不符，请检查锁定版本。",
    "runtime_modified": "MediaCrawler 工作区有改动，请使用干净的锁定版本。",
    "awaiting_login": "请在系统设置登录平台并重试。",
    "login_timeout": "登录等待超时，请在系统设置重新打开平台窗口并完成验证。",
    "challenge": "平台要求人工验证，请在系统设置重新登录并完成验证。",
    "rate_limited": "平台限制了请求，请稍后重试。",
    "timeout": "采集超时，请稍后重试。",
    "network": "平台网络请求失败，请检查连接后重试。",
    "tls_certificate": "平台连接的证书校验失败，请检查本机网络配置。",
    "profile_in_use": "平台浏览器配置正在使用，请关闭相关窗口后重试。",
    "browser_closed": "登录窗口已关闭，可重新发起登录。",
    "unexpected_response": "平台返回了无法识别的数据，请稍后重试。",
    "short_redirect_not_work": "分享短链未指向同平台的单条作品。",
    "short_unresolved": "分享短链无法解析，请换用有效的作品链接。",
    "work_unavailable": "平台未返回作品，请确认原帖仍可访问。",
    "douyin_detail_unavailable": "抖音作品页未返回可用的详情，请确认登录状态及原帖能否播放后重试。",
    "work_id_mismatch": "平台返回了另一条作品，请检查原始链接。",
    "account_unavailable": "平台未返回账号资料，请检查主页地址。",
    "platform_disabled": "平台已停用，请在系统设置启用后重试。",
    "tracking_inactive": "账号已暂停或移除，请恢复跟踪后重试。",
    "search_expired": "搜索历史已到期，请重新搜索。",
    "execution_interrupted": "采集执行中断，可重试。",
    "collector_protocol": "采集环境未返回有效结果，请检查本机运行环境后重试。",
    "media_has_no_audio_video": "媒体缺少视频或音轨，请换用有声视频。",
    "media_invalid": "平台返回的媒体文件无法读取，请确认原帖能否播放后重试。",
    "media_size_limit": "视频超过本机的媒体下载大小限制，请选用较小的视频。",
    "media_host_not_supported": "平台返回了当前采集器无法访问的媒体地址。",
    "no_audio": "媒体缺少有效音轨，请换用有声视频。",
    "tool_unavailable": "未找到采集所需工具。获取媒体时请确认 FFprobe 已安装。",
    "media_segment_layout": "当前作品的媒体分段格式无法读取，请换用单段视频。",
}


def problem(status, code, message):
    return queue.QueueError(status, code, message, None)


def read_settings(connection):
    values = dict(connection.execute(select(settings)).tuples().all())
    defaults = {
        "provider": "api", "base_url": "", "model": "",
        "codex_model": "", "codex_reasoning_effort": "",
    }
    saved = values.get("ai", {})
    values["ai"] = {key: saved.get(key, default) for key, default in defaults.items()}
    return values


def require_row(connection, table, item_id, message):
    row = connection.execute(select(table).where(table.c.id == item_id)).mappings().first()
    if row is None:
        raise problem(404, "not_found", message)
    return row


def require_platform(values, platform):
    if platform not in values["enabled_platforms"]:
        raise problem(409, "platform_disabled", ERRORS["platform_disabled"])


def input_link(value, kinds):
    try:
        link = parse_link(value)
    except LinkError as exc:
        code = str(exc)
        message = {
            "invalid_url": "请输入完整的 HTTP 或 HTTPS 平台链接。",
            "unsupported_platform": "当前支持 B 站、抖音和小红书链接。",
        }.get(code, "链接路径不符合平台的主页或作品格式。")
        raise problem(422, code, message) from None
    if link.kind not in kinds:
        message = (
            "请输入创作者主页链接。"
            if kinds == {"account"}
            else "这是创作者主页，请输入单条作品链接或分享短链。"
        )
        raise problem(422, "wrong_link_type", message)
    return link


def stamp(value):
    return datetime.fromtimestamp(value / 1000, UTC).isoformat() if value is not None else None


def measurement(row, previous, at):
    row = row or {}
    value = row.get("count")
    availability = row.get("availability")
    if type(value) is int and 0 <= value < 2**63:
        return {
            "count": value,
            "field": row.get("field"),
            "availability": "available",
            "collected_at": at,
        }
    if availability != "not_applicable" and previous and previous["count"] is not None:
        return previous
    return {
        "count": None,
        "field": row.get("field"),
        "availability": availability or "missing",
        "collected_at": None,
    }


def serialize_measurement(value):
    return {**value, "collected_at": stamp(value["collected_at"])} if value else None


def work_view(row, *, topic_id=None, workspace=None, include_images=False):
    cover_url = row["cover_url"]
    image_urls = row["image_urls"] or []
    work_url = f"/api/v1/works/{row['id']}"
    platform, native_id = row["platform"], row["native_id"]
    if workspace is not None:
        if covers.cached(workspace, platform, native_id):
            cover_url = work_url + "/cover"
        elif image_urls and covers.cached(workspace, platform, native_id, image_urls[0]):
            cover_url = work_url + "/images/0"
    result = {
        "id": row["id"],
        "platform": row["platform"],
        "kind": row["kind"],
        "title": row["title"],
        "author": row["author"],
        "body": row["body"],
        "original_url": row["original_url"],
        "cover_url": cover_url,
        "duration_seconds": row["duration_seconds"],
        "published_at": stamp(row["published_at"]),
        "created_at": stamp(row["created_at"]),
        "collected_at": stamp(row["collected_at"]),
        "metrics": {key: serialize_measurement(value) for key, value in row["metrics"].items()},
        "reads": serialize_measurement(row["reads"]),
        "topic_id": topic_id,
    }
    if include_images:
        result["image_urls"] = [
            f"{work_url}/images/{index}"
            if workspace is not None and covers.cached(workspace, platform, native_id, url) else url
            for index, url in enumerate(image_urls)
        ]
    return result


def clean_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def public_cover(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password:
            host = parsed.hostname or ""
            domains = (
                "hdslb.com",
                "bilivideo.com",
                "douyinpic.com",
                "byteimg.com",
                "xhscdn.com",
                "xiaohongshu.com",
                "douyinvod.com",
            )
            if any(host == domain or host.endswith("." + domain) for domain in domains):
                return parsed._replace(query="", fragment="").geturl()
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def upsert_work(connection, platform, data, at):
    link = parse_link(data["original_url"])
    if link.platform != platform or link.kind != "work" or link.native_id != data["native_id"]:
        raise collector.CollectionError("invalid_work_identity")
    old = (
        connection.execute(
            select(works).where(
                works.c.platform == platform,
                works.c.native_id == link.native_id,
            )
        )
        .mappings()
        .first()
    )
    old_metrics = old["metrics"] if old else {}
    supplied = data.get("metrics", {})
    kind = data["kind"]
    if old and kind == "unknown":
        kind = old["kind"]
    metrics = {
        key: measurement(
            {"availability": "not_applicable"}
            if key == "plays" and kind == "article"
            else supplied.get(key),
            old_metrics.get(key),
            at,
        )
        for key in METRICS
    }
    published = data.get("published_epoch_seconds")
    published_at = (
        int(published * 1000)
        if isinstance(published, (int, float)) and (0 < published < 253402300799)
        else None
    )
    collector_link = parse_link(data.get("collector_url") or link.public_url)
    if (collector_link.platform, collector_link.kind, collector_link.native_id) != (
        platform,
        "work",
        link.native_id,
    ):
        collector_link = link
    image_urls = None
    if platform == "xiaohongshu" and kind == "article":
        supplied_images = data.get("image_urls")
        if isinstance(supplied_images, list):
            image_urls = list(dict.fromkeys(
                url for value in supplied_images[:covers.MAX_ARTICLE_IMAGES]
                if (url := covers.xhs_image_url(value))
            ))
        if not image_urls and old:
            image_urls = old["image_urls"]
    values = {
        "kind": kind,
        "title": clean_text(data.get("title"), 500),
        "author": clean_text(data.get("author"), 200),
        "body": clean_text(data.get("body"), 50000),
        "cover_url": public_cover(data.get("cover_url")),
        "image_urls": image_urls,
        "published_at": published_at,
        "duration_seconds": data.get("duration_seconds"),
        "original_url": link.public_url,
        "collector_url": collector_link.url,
        "collected_at": at,
        "metrics": metrics,
        "reads": measurement(data.get("reads"), old["reads"] if old else None, at)
        if kind == "article"
        else None,
    }
    if old:
        for key in ("title", "author", "body", "cover_url", "published_at", "duration_seconds"):
            if values[key] is None:
                values[key] = old[key]
        if (
            collector_link.url == collector_link.public_url
            and old["collector_url"] != old["original_url"]
        ):
            values["collector_url"] = old["collector_url"]
        return (
            connection.execute(
                works.update()
                .where(works.c.id == old["id"])
                .values(
                    **values,
                )
                .returning(*works.c)
            )
            .mappings()
            .one()
        )
    values["title"] = values["title"] or link.native_id
    return (
        connection.execute(
            works.insert()
            .values(
                **values,
                id=uuid4().hex,
                native_id=link.native_id,
                platform=platform,
                created_at=at,
            )
            .returning(*works.c)
        )
        .mappings()
        .one()
    )


def discover(connection, work, kind, source_id, job_id, at, rank=None):
    row = connection.execute(
        insert(discoveries)
        .values(
            id=uuid4().hex,
            work_id=work["id"],
            kind=kind,
            source_id=source_id,
            job_id=job_id,
            account_id=source_id if kind == "scan" else None,
            search_id=source_id if kind == "search" else None,
            rank=rank,
            created_at=at,
        )
        .on_conflict_do_nothing(
            index_elements=[discoveries.c.kind, discoveries.c.source_id, discoveries.c.work_id],
        )
    )
    return row.rowcount == 1


def source_view(connection, row):
    label = "快速采集"
    if row["kind"] == "scan":
        label = connection.scalar(
            select(accounts.c.nickname).where(
                accounts.c.id == row["account_id"],
            )
        )
    elif row["kind"] == "search":
        label = connection.scalar(
            select(searches.c.keyword).where(
                searches.c.id == row["search_id"],
            )
        )
    return {
        "kind": row["kind"],
        "label": label,
        "source_id": row["source_id"],
        "job_id": row["job_id"],
        "discovered_at": stamp(row["created_at"]),
    }


def adopt(connection, work, source, at):
    result = connection.execute(
        insert(topics)
        .values(
            id=uuid4().hex,
            work_id=work["id"],
            source=source,
            snapshot=work_view(work),
            created_at=at,
            archived=False,
        )
        .on_conflict_do_nothing(index_elements=[topics.c.work_id])
        .returning(topics.c.id)
    ).scalar()
    return result or connection.scalar(select(topics.c.id).where(topics.c.work_id == work["id"]))


def session(connection, platform, state, at):
    connection.execute(
        insert(platform_sessions)
        .values(
            platform=platform,
            state=state,
            checked_at=at,
        )
        .on_conflict_do_update(
            index_elements=[platform_sessions.c.platform],
            set_={"state": state, "checked_at": at},
        )
    )


def metric_fields(items):
    fields = {metric: set() for metric in METRICS}
    for item in items:
        for metric, value in item.get("metrics", {}).items():
            if metric in fields and value.get("count") is not None and value.get("field"):
                fields[metric].add(value["field"])
    return {metric: sorted(paths) for metric, paths in fields.items()}


def capture_key(link):
    parsed = urlsplit(link.public_url)
    if link.native_id:
        return sha256(link.native_id.encode()).hexdigest()
    return sha256(f"{parsed.hostname}{parsed.path}".encode()).hexdigest()


def scan_submission(row, trigger):
    return queue.Submission(
        "account_scan",
        row["id"],
        row["nickname"],
        row["platform"],
        {
            "account_id": row["id"],
            "trigger": trigger,
        },
    )


def submit_scan(connection, account, trigger):
    job_id = queue.submit_in(connection, scan_submission(account, trigger))
    connection.execute(
        accounts.update().where(accounts.c.id == account["id"]).values(last_job_id=job_id)
    )
    return job_id


def scheduled_accounts(values):
    selected = values["scan"].get("platforms")
    platforms = [name for name in values["enabled_platforms"] if not selected or name in selected]
    return select(accounts).where(
        accounts.c.tracking == "active", accounts.c.platform.in_(platforms),
    )


def pending_scheduled_accounts(statement, local_date):
    dispatched = select(scan_dispatches.c.account_id).where(
        scan_dispatches.c.local_date == local_date
    )
    return statement.where(accounts.c.id.not_in(dispatched))


def scan_plan_status(connection, values):
    plan = values["scan"]
    eligible = scheduled_accounts(values)
    count = connection.scalar(select(func.count()).select_from(eligible.subquery()))
    next_run = None
    if plan["enabled"] and count:
        now = datetime.now()
        hour, minute = map(int, plan["time"].split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            pending = pending_scheduled_accounts(eligible, now.date().isoformat())
            pending_id = connection.scalar(pending.with_only_columns(accounts.c.id).limit(1))
            target = now if pending_id else target + timedelta(days=1)
        next_run = target.astimezone().isoformat(timespec="minutes")
    last_job = connection.execute(
        select(jobs).where(
            jobs.c.parent_id.is_(None), jobs.c.kind == "account_scan",
            jobs.c.parameters["trigger"].as_string() == "scheduled",
        ).order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(1)
    ).mappings().first()
    return count, next_run, last_job


def schedule(engine, local_now=None):
    local_now = local_now or datetime.now().astimezone()
    date = local_now.date().isoformat()
    at = int(local_now.timestamp() * 1000)
    with write_transaction(engine) as connection:
        values = read_settings(connection)
        plan = values["scan"]
        if plan["enabled"] and local_now.strftime("%H:%M") >= plan["time"]:
            statement = pending_scheduled_accounts(scheduled_accounts(values), date)
            eligible = connection.execute(
                statement.order_by(accounts.c.created_at, accounts.c.id),
            ).mappings().all()
            for account in eligible:
                connection.execute(
                    scan_dispatches.insert().values(local_date=date, account_id=account["id"])
                )
                submit_scan(connection, account, "scheduled")
        connection.execute(
            searches.delete().where(
                or_(
                    searches.c.hidden,
                    searches.c.created_at < at - values["retention"]["search_days"] * DAY_MS,
                ),
                ~select(jobs.c.id)
                .where(jobs.c.id == searches.c.job_id, jobs.c.status.in_(ACTIVE))
                .exists(),
            )
        )


def persist_result(connection, job, params, result, at):
    kind, platform = job["kind"], job["platform"]
    if kind == "platform_login":
        session(connection, platform, "ready", at)
        return {"session": "ready"}
    if kind == "account_resolve":
        link = parse_link(params["url"])
        if result["native_id"] != link.native_id:
            raise collector.CollectionError("invalid_account_identity")
        nickname = clean_text(result.get("nickname"), 200) or link.native_id
        statement = insert(accounts).values(
            id=uuid4().hex,
            platform=platform,
            native_id=link.native_id,
            nickname=nickname,
            profile_url=link.public_url,
            collector_url=link.url,
            note=params["note"],
            tracking="active",
            created_at=at,
            lookback_days=params["lookback_days"],
            scan_limit=params["scan_limit"],
        )
        account_id = connection.scalar(
            statement.on_conflict_do_update(
                index_elements=[accounts.c.platform, accounts.c.native_id],
                set_={
                    "nickname": nickname,
                    "profile_url": link.public_url,
                    "collector_url": link.url
                    if link.url != link.public_url
                    else accounts.c.collector_url,
                    "tracking": func.iif(
                        accounts.c.tracking == "removed", "active", accounts.c.tracking
                    ),
                },
            ).returning(accounts.c.id)
        )
        session(connection, platform, "ready", at)
        return {"account_id": account_id}
    if kind == "quick_capture":
        work = upsert_work(connection, platform, result, at)
        discover(connection, work, "quick", job["id"], job["id"], at)
        source = {
            "kind": "quick",
            "label": "快速采集",
            "source_id": job["id"],
            "job_id": job["id"],
            "discovered_at": stamp(at),
        }
        topic_id = adopt(connection, work, source, at)
        connection.execute(
            insert(capture_aliases)
            .values(
                platform=platform,
                link_key=capture_key(parse_link(params["url"])),
                native_id=work["native_id"],
            )
            .on_conflict_do_update(
                index_elements=[capture_aliases.c.platform, capture_aliases.c.link_key],
                set_={"native_id": work["native_id"]},
            )
        )
        session(connection, platform, "ready", at)
        return {
            "work_id": work["id"],
            "topic_id": topic_id,
            "metric_fields": metric_fields([result]),
        }
    if kind == "media_fetch":
        session(connection, platform, "ready", at)
        return {"work_id": params["work_id"], **result}
    scan = kind == "account_scan"
    source_id = params["account_id"] if scan else params["search_id"]
    source_kind = "scan" if scan else "search"
    next_rank = (
        None
        if scan
        else connection.scalar(
            select(func.coalesce(func.max(discoveries.c.rank), -1) + 1)
            .select_from(discoveries.join(works))
            .where(discoveries.c.search_id == source_id, works.c.platform == platform)
        )
    )
    added = 0
    for data in result["items"]:
        work = upsert_work(connection, platform, data, at)
        if discover(connection, work, source_kind, source_id, job["id"], at, next_rank):
            added += 1
            if next_rank is not None:
                next_rank += 1
    update_session(connection, platform, result.get("partial_error"), at)
    return {
        "count": len(result["items"]),
        "added": added,
        "updated": len(result["items"]) - added,
        "metric_fields": metric_fields(result["items"]),
        "account_id" if scan else "search_id": source_id,
    }


def update_session(connection, platform, code, at):
    states = {
        None: "ready",
        "awaiting_login": "login_required",
        "login_timeout": "login_required",
        "challenge": "challenge",
        "rate_limited": "rate_limited",
    }
    if code in states:
        session(connection, platform, states[code], at)


def request_for(connection, job):
    params = job["parameters"]
    platform = job["platform"]
    values = read_settings(connection)
    if platform not in values["enabled_platforms"]:
        raise collector.CollectionError("platform_disabled")
    request = {
        "operation": OPERATIONS[job["kind"]],
        "platform": platform,
        "attempt": job["attempt_count"],
    }
    match job["kind"]:
        case "account_scan":
            account = require_row(connection, accounts, params["account_id"], "账号不存在。")
            if account["tracking"] != "active":
                raise collector.CollectionError("tracking_inactive")
            request.update(
                url=account["collector_url"],
                limit=account["scan_limit"] or values["scan"]["limit"],
                cutoff_seconds=(
                    queue.now_ms()
                    - DAY_MS * (account["lookback_days"] or values["scan"]["lookback_days"])
                )
                // 1000,
            )
        case "search":
            item = (
                connection.execute(
                    select(searches).where(
                        searches.c.id == params["search_id"],
                    )
                )
                .mappings()
                .first()
            )
            if item is None:
                raise collector.CollectionError("search_expired")
            request.update(keyword=item["keyword"], limit=values["scan"]["limit"])
        case "media_fetch":
            work = require_row(connection, works, params["work_id"], "作品不存在。")
            request["url"] = work["collector_url"]
        case "platform_login":
            pass
        case _:
            request["url"] = params["url"]
    return request


def execute(engine, config, job, stop):
    params = job["parameters"]
    kind, platform = job["kind"], job["platform"]
    temporary = None
    target = None
    committed = False
    try:
        phase_message = "请在本机浏览器扫码或完成平台验证。" if kind == "platform_login" else None
        if not queue.checkpoint(engine, job, OPERATIONS[kind], message=phase_message):
            raise collector.CollectionError("execution_interrupted")
        with engine.connect() as connection:
            request = request_for(connection, job)
        if kind == "media_fetch":
            temporary = media.attempt_path(config, job, "download")
            target = temporary.with_suffix(".media")
            request["target"] = str(temporary)
        result = collector.run(
            config,
            request,
            lambda: not stop.is_set() and queue.checkpoint(engine, job),
        )
        code = result.get("partial_error")
        count = len(result.get("items", []))
        message = (
            ERRORS.get(code, "部分结果已保存，可重试。")
            if code
            else (
                f"已获取 {count} 条作品"
                if "items" in result and count
                else "无匹配作品"
                if "items" in result
                else "任务已完成"
            )
        )

        def persist(connection, at):
            if temporary and target:
                temporary.replace(target)
                result["media_path"] = target.relative_to(config.workspace_dir).as_posix()
                media.record(connection, config, params["work_id"], "media", target, at)
            return persist_result(connection, job, params, result, at)

        committed = queue.finish(
            engine,
            job,
            "partially_succeeded" if code else "succeeded",
            message,
            code=code,
            persist=persist,
        )
    except Exception as exc:
        code = str(exc) if isinstance(exc, collector.CollectionError) else "execution_error"
        if kind == "platform_login" and code in {"timeout", "awaiting_login"}:
            code = "login_timeout"
        if code not in ERRORS and not code.startswith(("upstream_", "media_")):
            code = "execution_error"
        status = {
            "awaiting_login": "waiting_login",
            "login_timeout": "waiting_login",
            "challenge": "waiting_login",
            "execution_interrupted": "interrupted",
        }.get(code, "failed")

        def persist_error(connection, at):
            update_session(connection, platform, code, at)

        queue.finish(
            engine,
            job,
            status,
            ERRORS.get(code, "采集失败，可查看任务日志后重试。"),
            code=code,
            persist=persist_error,
        )
    finally:
        if isinstance(temporary, Path):
            temporary.unlink(missing_ok=True)
        if target is not None and not committed:
            target.unlink(missing_ok=True)
