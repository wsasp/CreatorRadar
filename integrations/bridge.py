"""JSON entry point executed by the pinned MediaCrawler interpreter."""

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import nullcontext
from html import unescape
from pathlib import Path

from common import ProbeError
from crawler import (
    Browser, account_details, at, canonical_input, detail, douyin_profile_posts, download, failure_code,
    normalize, offline, prompt_login, resolve_short, wait_session,
)
from runtime_guard import validate_runtime

# The application and bridge use the same URL policy without importing business dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from creator_radar.covers import MAX_ARTICLE_IMAGES, xhs_image_url
from creator_radar.links import LinkError, parse_link
from cover_cache import cache_covers


def checked(platform, url, kind):
    try:
        link = parse_link(url)
    except LinkError:
        raise ProbeError("short_redirect_not_work") from None
    if link.platform != platform or link.kind != kind:
        raise ProbeError("short_redirect_not_work")
    return link.native_id


def unique_urls(candidates):
    return list(dict.fromkeys("https:" + url if url.startswith("//") else url
                              for url in candidates if isinstance(url, str) and url))


def xhs_image_candidates(image):
    urls = [at(image, key) for key in ("url_default", "url", "url_pre")]
    variants = at(image, "info_list")
    if isinstance(variants, list):
        urls.extend(at(variant, "url") for variant in variants)
    return unique_urls(urls)


def xhs_gallery(images):
    gallery = {}
    for image in images[:MAX_ARTICLE_IMAGES]:
        candidates = [url for url in xhs_image_candidates(image) if xhs_image_url(url)]
        if candidates:
            identity = xhs_image_url(candidates[0])
            gallery[identity] = unique_urls(gallery.get(identity, []) + candidates)
    return gallery


def cover_candidates(platform, item, first_image):
    if platform == "bilibili":
        candidates = [at(item, "pic")]
    elif platform == "douyin":
        groups = [at(item, f"video.{field}.url_list")
                  for field in ("cover", "origin_cover", "raw_cover")]
        groups.extend((at(first_image, "url_list"), at(first_image, "display_image.url_list")))
        candidates = [url for group in groups if isinstance(group, list) for url in group]
    else:
        candidates = xhs_image_candidates(at(item, "cover")) + xhs_image_candidates(first_image)
    return unique_urls(candidates)


def work_row(platform, raw, *, with_gallery=False):
    row = normalize(platform, raw)
    item = raw.get("View", raw) if platform == "bilibili" else raw.get("note_card") or raw
    title = at(item, "title") or at(item, "display_title") or at(item, "desc")
    body = at(item, "desc") or at(item, "description")
    if platform == "bilibili" and isinstance(title, str):
        title = unescape(re.sub(r"</?em\b[^>]*>", "", title))
    primary_images, alternate_images = (
        ("image_list", "images") if platform == "xiaohongshu" else ("images", "image_list")
    )
    images = at(item, primary_images) or at(item, alternate_images)
    images = images if isinstance(images, list) else []
    first_image = images[0] if images else None
    author, duration = {
        "bilibili": (at(item, "owner.name") or at(item, "author"), at(item, "duration")),
        "douyin": (at(item, "author.nickname"),
                   (at(item, "video.duration") or 0) // 1000
                   if isinstance(at(item, "video.duration"), int) else None),
        "xiaohongshu": (at(item, "user.nickname") or at(item, "user.nick_name"),
                        at(item, "video.capa.duration")),
    }[platform]
    candidates = cover_candidates(platform, item, first_image)
    cover = candidates[0].split("?", 1)[0].split("#", 1)[0] if candidates else None
    if platform == "xiaohongshu":
        if with_gallery and row["kind"] == "article":
            row["image_candidates"] = xhs_gallery(images)
        signed = canonical_input(platform, raw, row)
        if signed == row["original_url"] and item is not raw:
            signed = canonical_input(platform, item, row)
    else:
        signed = row["original_url"]
    row.update(title=title, body=body, author=author, cover_url=cover, cover_candidates=candidates,
               collector_url=signed, duration_seconds=duration if type(duration) is int else None)
    return row


async def page(client, platform, request, index, cursor, search_id):
    scan = request["operation"] == "scan"
    url = request.get("url")
    keyword = request.get("keyword")
    limit = request["limit"]
    if platform == "bilibili":
        from media_platform.bilibili.field import SearchOrderType

        size = min(limit, 20)
        if scan:
            response = await client.get_creator_videos(
                checked(platform, url, "account"), index, size, SearchOrderType.LAST_PUBLISH.value,
            )
            rows = at(response, "list.vlist")
            total = at(response, "page.count")
            more = index * size < total if isinstance(total, int) else len(rows or []) == size
        else:
            response = await client.search_video_by_keyword(keyword, page=index, page_size=size)
            rows = response.get("result", [] if response.get("numResults") == 0 else None)
            more = index < response.get("numPages", index)
        return rows, index + 1 if more else None, search_id
    if platform == "douyin":
        response = await client.search_info_by_keyword(keyword, offset=int(cursor or 0), search_id=search_id)
        search_id = at(response, "extra.logid") or at(response, "log_pb.impr_id") or search_id
        entries = response.get("data")
        rows = None if not isinstance(entries, list) else [
            row for entry in entries if isinstance(entry, dict)
            for row in ([entry["aweme_info"]] if entry.get("aweme_info")
                        else (at(entry, "aweme_mix_info.mix_items") or []))
        ]
        next_cursor = response.get("cursor") or int(cursor or 0) + len(entries or [])
        if response.get("status_code", 0) not in (0, None):
            raise ProbeError(failure_code(RuntimeError(str(response.get("status_msg", "")))))
        if at(response, "search_nil_info.search_nil_type") in {"verify_check", "verify_type"}:
            raise ProbeError("challenge")
        if rows is None and response.get("has_more") == 0 and "data" in response:
            rows = []
        return rows, next_cursor if response.get("has_more") else None, search_id
    if scan:
        from media_platform.xhs.help import parse_creator_info_from_url

        creator = parse_creator_info_from_url(url)
        response = await client.get_notes_by_creator(
            checked(platform, url, "account"), cursor, min(limit, 30), creator.xsec_token,
            creator.xsec_source or "pc_feed",
        )
        return response.get("notes"), response.get("cursor") if response.get("has_more") else None, search_id
    from media_platform.xhs.help import get_search_id

    search_id = search_id or get_search_id()
    response = await client.get_note_by_keyword(
        keyword, search_id=search_id, page=index, page_size=min(limit, 20),
    )
    rows = response.get("items", [] if response.get("has_more") is False else None)
    return rows, index + 1 if response.get("has_more") else None, search_id


async def collect(client, platform, request):
    rows = {}
    index, cursor, search_id = 1, "", ""
    cutoff = request.get("cutoff_seconds")
    posts = nullcontext()
    if platform == "douyin" and request["operation"] == "scan":
        native = checked(platform, request["url"], "account")
        posts = douyin_profile_posts(client.playwright_page, native)
    try:
        async with asyncio.timeout(95), posts as read_posts:
            for _ in range(10):
                if read_posts:
                    items, next_cursor = await read_posts(cursor)
                else:
                    items, next_cursor, search_id = await page(
                        client, platform, request, index, cursor, search_id,
                    )
                if not isinstance(items, list):
                    raise ProbeError("unexpected_response")
                dates = []
                for raw in items:
                    if not isinstance(raw, dict) or raw.get("model_type") in {"hot_query", "rec_query"}:
                        continue
                    try:
                        row = work_row(platform, raw)
                    except ProbeError:
                        continue
                    if row["native_id"] in rows:
                        continue
                    if cutoff and row["published_epoch_seconds"] and row["published_epoch_seconds"] < cutoff:
                        dates.append(row["published_epoch_seconds"])
                        continue
                    if platform in {"bilibili", "xiaohongshu"}:
                        try:
                            raw_detail = await detail(client, platform, row["collector_url"], validator=checked)
                            enriched = work_row(platform, raw_detail, with_gallery=True)
                            if enriched["native_id"] == row["native_id"]:
                                for key in ("title", "body", "author", "cover_url", "duration_seconds",
                                            "published_epoch_seconds"):
                                    row[key] = enriched[key] or row[key]
                                row["cover_candidates"] = list(dict.fromkeys(
                                    enriched["cover_candidates"] + row["cover_candidates"],
                                ))
                                if enriched.get("image_candidates"):
                                    row["image_candidates"] = enriched["image_candidates"]
                                for key, metric in enriched["metrics"].items():
                                    if metric["count"] is not None or metric["availability"] == "not_applicable":
                                        row["metrics"][key] = metric
                                if enriched["kind"] != "unknown":
                                    row["kind"] = enriched["kind"]
                                row["reads"] = enriched["reads"]
                        except Exception as exc:
                            code = failure_code(exc)
                            if code in {"awaiting_login", "challenge", "rate_limited"}:
                                rows[row["native_id"]] = row
                                raise
                    published = row["published_epoch_seconds"]
                    dates.append(published)
                    if cutoff and published is not None and published < cutoff:
                        continue
                    rows[row["native_id"]] = row
                    if len(rows) == request["limit"]:
                        break
                past_cutoff = cutoff and platform != "xiaohongshu" and dates and all(
                    published and published < cutoff for published in dates
                )
                if (len(rows) == request["limit"] or next_cursor is None or next_cursor == cursor
                        or past_cutoff):
                    break
                index += 1
                cursor = next_cursor
                await asyncio.sleep(1)
    except Exception as exc:
        if not rows:
            raise
        return {"items": list(rows.values()), "partial_error": failure_code(exc)}
    return {"items": list(rows.values())}


async def execute(request):
    from playwright.async_api import async_playwright

    platform = request["platform"]
    operation = request["operation"]
    if operation == "offline":
        async with async_playwright() as playwright:
            return await offline(playwright, report=False)
    workspace = Path(request["workspace"])
    profile = workspace / "profiles" / platform
    async with async_playwright() as playwright:
        async with Browser(playwright, platform, profile=profile) as browser:
            if operation == "login":
                await prompt_login(browser)
                await wait_session(browser, 600)
            else:
                if not await browser.signed_in():
                    raise ProbeError("awaiting_login")
                client = await browser.client()
                if operation == "account":
                    return await account_details(client, platform, request["url"])
                if operation in {"scan", "search"}:
                    result = await collect(client, platform, request)
                    await cache_covers(workspace, platform, client, result["items"])
                    return result
                url = request["url"]
                if parse_link(url).kind == "short":
                    url = await resolve_short(client, platform, url, validator=checked)
                raw = await detail(client, platform, url, validator=checked)
                row = work_row(platform, raw, with_gallery=operation == "detail")
                native = parse_link(url).native_id
                if native != row["native_id"] and not (
                    platform == "bilibili" and native.startswith("av")
                    and str(at(raw, "View.aid")) == native[2:]
                ):
                    raise ProbeError("work_id_mismatch")
                if platform == "xiaohongshu":
                    row["collector_url"] = parse_link(url).url
                if operation == "detail":
                    await cache_covers(workspace, platform, client, [row])
                    return row
                if row["kind"] != "video":
                    raise ProbeError("video_sample_required")
                target = Path(request["target"])
                await download(platform, client, raw, target)
                return {"bytes": target.stat().st_size, "has_audio": True}
        async with Browser(playwright, platform, profile=profile) as browser:
            await wait_session(browser, 25)
    return {"state": "ready"}


def main():
    runtime = Path(sys.argv[1]).resolve()
    request = json.load(sys.stdin)
    os.chdir(runtime)
    validate_runtime(runtime)
    if sys.version_info[:2] != (3, 12):
        raise ProbeError("python_version_mismatch")
    sys.path.insert(0, str(runtime))
    import config

    config.ENABLE_GET_COMMENTS = False
    config.ENABLE_GET_SUB_COMMENTS = False
    config.MAX_CONCURRENCY_NUM = 1
    return asyncio.run(execute(request))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    output = sys.stdout
    try:
        # Upstream libraries may print diagnostics. Only the JSON response goes to the Worker.
        sys.stdout = sys.stderr
        reply = {"data": main()}
    except Exception as exc:
        reply = {"error": failure_code(exc)}
    print(json.dumps(reply, ensure_ascii=False), file=output)
