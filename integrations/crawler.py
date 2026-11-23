"""Run using the Python environment of the pinned MediaCrawler checkout."""

import argparse
import asyncio
import importlib
import importlib.metadata
import ipaddress
import json
import logging
import os
import re
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from common import LIMIT, REVISION, STATE, ProbeError, audio_probe, evidence, extract_audio, run_tool
from runtime_guard import validate_runtime


PLATFORMS = {
    "bilibili": ("bilibili.com", "b23.tv", "bilibili", "BilibiliClient"),
    "douyin": ("douyin.com", "v.douyin.com", "douyin", "DouYinClient"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhs", "XiaoHongShuClient"),
}
DEFAULTS = {
    "bilibili": ("https://space.bilibili.com/434377496", "https://www.bilibili.com/video/BV1dwuKzmE26",
                 "https://b23.tv/9ZDFHda"),
    "douyin": ("https://www.douyin.com/user/MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE",
               "https://www.douyin.com/video/7525538910311632128", "https://v.douyin.com/drIPtQ_WPWY/"),
    "xiaohongshu": ("https://www.xiaohongshu.com/user/profile/5f58bd990000000001003753", "",
                    "http://xhslink.com/a/K8EhFQD66BwZ"),
}
OPERATIONS = ("account", "scan", "search", "detail", "original", "short", "media", "article")
METRICS = ("plays", "likes", "comments", "favorites", "shares")
MAX_MEDIA_BYTES = 80 * 1024 * 1024
DOUYIN_PAGE_HOSTS = {"www.douyin.com", "www-hj.douyin.com"}


def match_host(host, domain):
    return host == domain or host.endswith("." + domain)


def public_url(url):
    return urlsplit(url)._replace(query="", fragment="").geturl()


def checked_url(platform, url, kind):
    domain, short, *_ = PLATFORMS[platform]
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ProbeError("invalid_url") from None
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or port not in {None, 80, 443} or not parsed.hostname):
        raise ProbeError("invalid_url")
    host = parsed.hostname
    if kind == "short":
        if not match_host(host, short) or (short == "v.douyin.com" and host != short):
            raise ProbeError("invalid_short_url")
        pattern = r"/+(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+/?"
    elif not match_host(host, domain):
        raise ProbeError("wrong_platform")
    elif kind == "account":
        if platform == "bilibili" and host != "space.bilibili.com":
            raise ProbeError("invalid_account_url")
        pattern = {"bilibili": r"/(\d+)(?:/.*)?", "douyin": r"/user/([\w-]+)/?",
                   "xiaohongshu": r"/user/profile/([0-9a-f]{24})/?"}[platform]
    else:
        pattern = {"bilibili": r"/video/(BV[0-9A-Za-z]+|av\d+)/?",
                   "douyin": r"/(?:video|note|share/video|share/note)/(\d+)/?",
                   "xiaohongshu": r"/(?:explore|item|discovery/item)/([0-9a-f]{24})/?"}[platform]
    match = re.fullmatch(pattern, parsed.path, re.ASCII)
    if not match:
        raise ProbeError(f"invalid_{kind}_url")
    return match[1] if match.groups() else None


def at(data, path):
    return field_at(data, path)[1]


def field_at(data, path):
    actual = []
    for key in path.split("."):
        if not isinstance(data, dict):
            return None, None
        if key not in data:
            key = re.sub(r"_([a-z])", lambda m: m[1].upper(), key)
        actual.append(key)
        data = data.get(key)
    return ".".join(actual), data


def count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return int(value.strip())
    return None


def metric(data, paths):
    unknown = None
    for path in paths:
        field, value = field_at(data, path)
        if count(value) is not None:
            return {"field": field, "count": count(value), "availability": "available"}
        if value is not None:
            unknown = field
    return {"field": unknown, "count": None, "availability": "unparsed" if unknown else "missing"}


def normalize(platform, raw):
    if platform == "bilibili":
        item = raw.get("View", raw)
        native = item.get("bvid")
        paths = [("stat.view", "play"), ("stat.like",), ("stat.reply", "comment"),
                 ("stat.favorite", "favorites"), ("stat.share",)]
        read_path = ()
        kind = "video"
    elif platform == "douyin":
        item = raw
        native = item.get("aweme_id")
        paths = [("statistics.play_count",), ("statistics.digg_count",), ("statistics.comment_count",),
                 ("statistics.collect_count",), ("statistics.share_count",)]
        kind = "article" if item.get("images") or item.get("image_post_info") else "video" if item.get("video") else "unknown"
        read_path = ("statistics.read_count", "statistics.view_count")
    else:
        item = raw.get("note_card") or raw
        native = at(item, "note_id") or raw.get("id") or item.get("id")
        paths = [("interact_info.play_count",), ("interact_info.liked_count", "liked_count"),
                 ("interact_info.comment_count",), ("interact_info.collected_count",), ("interact_info.share_count",)]
        kind = {"normal": "article", "video": "video"}.get(item.get("type"), "unknown")
        read_path = ("interact_info.view_count", "interact_info.read_count")
    metrics = {name: metric(item, fields) for name, fields in zip(METRICS, paths, strict=True)}
    if kind == "article":
        metrics["plays"] = {"count": None, "field": None, "availability": "not_applicable"}
    prefix = {"bilibili": "https://www.bilibili.com/video/", "douyin": "https://www.douyin.com/",
              "xiaohongshu": "https://www.xiaohongshu.com/explore/"}[platform]
    if platform == "douyin":
        prefix += "note/" if kind == "article" else "video/"
    if not native or not re.fullmatch(r"[A-Za-z0-9_-]+", str(native)):
        raise ProbeError("missing_work_id")
    reads = metric(item, read_path) if kind == "article" else metric({}, ())
    scope = "View" if platform == "bilibili" and "View" in raw else "note_card" if "note_card" in raw else None
    if scope:
        for measure in [*metrics.values(), reads]:
            if measure["field"]:
                measure["field"] = f"{scope}.{measure['field']}"
    title = at(item, "title") or at(item, "display_title") or at(item, "desc")
    body = at(item, "desc") or at(item, "description")
    timestamp_fields = {"bilibili": ("pubdate", "created"), "douyin": ("create_time",),
                        "xiaohongshu": ("time", "publish_time")}[platform]
    published = metric(item, timestamp_fields)["count"]
    return {"native_id": str(native), "original_url": prefix + str(native), "kind": kind,
            "title_available": bool(title), "body_available": bool(body),
            "published_epoch_seconds": published / 1000 if published and published > 100_000_000_000 else published,
            "metrics": metrics, "reads": reads}


class Browser:
    def __init__(self, playwright, platform, guest=False, profile=None):
        self.playwright = playwright
        self.platform = platform
        self.domain, _, self.module, self.client_name = PLATFORMS[platform]
        self.home = f"https://www.{self.domain}/"
        self.guest = guest
        self.profile = profile

    async def __aenter__(self):
        profile = self.profile or STATE / ("guest-profiles" if self.guest else "profiles") / self.platform
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(profile), headless=False, timeout=45000,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        try:
            await self.page.goto(self.home, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            await self.context.close()
            raise
        return self

    async def __aexit__(self, *_):
        await self.context.close()

    async def client(self):
        cls = getattr(importlib.import_module(f"media_platform.{self.module}.client"), self.client_name)
        user_agent = await self.page.evaluate("navigator.userAgent")
        client = cls(proxy=None, headers={"Origin": self.home.rstrip("/"), "Referer": self.home,
                                         "User-Agent": user_agent, "Content-Type": "application/json"},
                     playwright_page=self.page, cookie_dict={})
        await client.update_cookies(self.context)
        return client

    async def signed_in(self):
        if self.page.is_closed():
            raise ProbeError("browser_closed")
        client = await self.client()
        if self.platform == "douyin":
            return await client.pong(self.context)
        if self.platform == "xiaohongshu":
            status = await client.query_self()
            # Self-info may legitimately return a successful response for guest sessions.
            guest = at(status, "data.guest")
            if guest is True:
                return False
            return at(status, "data.result.success") is True
        return await client.pong()


async def login(playwright, args):
    evidence(args.platform, "session_restore", "blocked", error="login_required")
    async with Browser(playwright, args.platform) as browser:
        evidence(args.platform, "login", "awaiting_login", browser="headed Chromium")
        await prompt_login(browser)
        await wait_session(browser, args.wait)
        evidence(args.platform, "login", "pass", browser="headed Chromium")
    try:
        async with Browser(playwright, args.platform) as browser:
            await wait_session(browser, 30)
    except Exception as exc:
        code = failure_code(exc)
        evidence(args.platform, "session_restore", "fail", error=code)
        return False
    evidence(args.platform, "session_restore", "pass")
    return True


async def prompt_login(browser):
    if await browser.signed_in():
        return
    if browser.platform == "douyin":
        from media_platform.douyin.login import DouYinLogin

        try:
            await DouYinLogin("qrcode", browser.context, browser.page).popup_login_dialog()
        except Exception:
            pass  # The login entry is also available on the official page.
        return
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    selector = {
        "bilibili": ".right-entry__outside.go-login-btn",
        "xiaohongshu": "xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/div[1]/button",
    }[browser.platform]
    try:
        await browser.page.locator(selector).first.click(timeout=3000)
    except PlaywrightTimeout:
        pass  # Some visits already show the login prompt.


async def wait_session(browser, seconds):
    try:
        async with asyncio.timeout(seconds):
            while not await browser.signed_in():
                await asyncio.sleep(5)
    except TimeoutError:
        raise ProbeError("awaiting_login") from None


def canonical_input(platform, raw, row):
    if platform != "xiaohongshu":
        return row["original_url"]
    return xhs_link(row["original_url"], at(raw, "xsec_token"), at(raw, "xsec_source") or "pc_search")


def xhs_link(url, token, source):
    from urllib.parse import urlencode

    return f"{url}?{urlencode({'xsec_token': token, 'xsec_source': source})}" if token else url


async def list_results(client, platform, operation, account, keyword):
    if operation == "scan":
        native = checked_url(platform, account, "account")
        if platform == "bilibili":
            from media_platform.bilibili.field import SearchOrderType

            rows = at(await client.get_creator_videos(native, 1, LIMIT, SearchOrderType.LAST_PUBLISH.value),
                      "list.vlist")
        elif platform == "douyin":
            async with douyin_profile_posts(client.playwright_page, native) as read_posts:
                rows, _ = await read_posts("")
        else:
            from media_platform.xhs.help import parse_creator_info_from_url

            parsed = parse_creator_info_from_url(account)
            rows = at(await client.get_notes_by_creator(native, "", LIMIT, parsed.xsec_token,
                                                         parsed.xsec_source or "pc_feed"), "notes")
    elif platform == "bilibili":
        rows = at(await client.search_video_by_keyword(keyword, page_size=LIMIT), "result")
    elif platform == "douyin":
        raw = at(await client.search_info_by_keyword(keyword), "data")
        rows = [] if isinstance(raw, list) else None
        if rows is not None:
            for entry in raw:
                if isinstance(entry, dict):
                    if entry.get("aweme_info"):
                        rows.append(entry["aweme_info"])
                    else:
                        rows.extend(at(entry, "aweme_mix_info.mix_items") or [])
    else:
        from media_platform.xhs.help import get_search_id

        rows = at(await client.get_note_by_keyword(keyword, search_id=get_search_id(), page_size=LIMIT), "items")
    if not isinstance(rows, list):
        raise ProbeError("unexpected_response")
    return [r for r in rows if isinstance(r, dict) and r.get("model_type") not in {"hot_query", "rec_query"}][:LIMIT]


def check_douyin_profile_status(status):
    if status >= 400:
        raise ProbeError({
            401: "awaiting_login",
            403: "douyin_scan_forbidden",
            404: "account_unavailable",
            429: "rate_limited",
        }.get(status, "douyin_scan_unavailable"))


@asynccontextmanager
async def douyin_profile_posts(page, native):
    """Read the posts delivered to the signed-in Douyin profile page, including prefetched pages."""
    responses = {}
    arrived = asyncio.Event()

    def observe(response):
        parsed = urlsplit(response.url)
        if (parsed.scheme != "https" or parsed.netloc not in DOUYIN_PAGE_HOSTS
                or parsed.path != "/aweme/v1/web/aweme/post/" or response.request.method != "GET"):
            return
        params = parse_qs(parsed.query, keep_blank_values=True)
        if params.get("sec_user_id") != [native]:
            return
        cursor = params.get("max_cursor", ["0"])
        if len(cursor) == 1:
            responses.setdefault(cursor[0] or "0", response)
            arrived.set()

    async def read_posts(cursor):
        cursor = str(cursor or 0)
        try:
            async with asyncio.timeout(22):
                if cursor == "0":
                    opened = await page.goto(f"https://www.douyin.com/user/{native}",
                                             wait_until="domcontentloaded", timeout=0)
                    if opened:
                        check_douyin_profile_status(opened.status)
                elif cursor not in responses:
                    links = page.locator("a[href*='/video/']:visible, a[href*='/note/']:visible")
                    await links.last.scroll_into_view_if_needed(timeout=0)
                while cursor not in responses:
                    arrived.clear()
                    await arrived.wait()
                response = responses.pop(cursor)
                check_douyin_profile_status(response.status)
                try:
                    payload = await response.json()
                except ValueError:
                    raise ProbeError("douyin_scan_unavailable") from None
        except TimeoutError:
            verify = page.get_by_text(re.compile(r"^(请完成安全验证|安全验证|拖动滑块完成验证)$"))
            if await verify.first.is_visible():
                raise ProbeError("challenge") from None
            login = page.get_by_text(re.compile(r"^(登录后查看作品|登录后查看更多|请先登录)$"))
            if await login.first.is_visible():
                raise ProbeError("awaiting_login") from None
            raise ProbeError("douyin_scan_unavailable") from None

        if not isinstance(payload, dict):
            raise ProbeError("douyin_scan_unavailable")
        if payload.get("status_code", 0) not in (0, None):
            code = failure_code(RuntimeError(str(payload.get("status_msg", ""))))
            raise ProbeError(code if code in {"challenge", "awaiting_login", "rate_limited"}
                             else "douyin_scan_unavailable")
        rows = payload.get("aweme_list")
        more = payload.get("has_more")
        if rows is None and more == 0 and "aweme_list" in payload:
            rows = []
        next_cursor = payload.get("max_cursor") if more else None
        if not isinstance(rows, list) or (more and type(next_cursor) not in {str, int}):
            raise ProbeError("douyin_scan_unavailable")
        return rows, next_cursor

    page.on("response", observe)
    try:
        yield read_posts
    finally:
        page.remove_listener("response", observe)


async def douyin_page_detail(page, url, native):
    responses = asyncio.Queue()

    def observe(response):
        parsed = urlsplit(response.url)
        if (parsed.scheme == "https" and parsed.netloc in DOUYIN_PAGE_HOSTS
                and parsed.path == "/aweme/v1/web/aweme/detail/"
                and response.request.method == "GET"
                and parse_qs(parsed.query).get("aweme_id") == [native]):
            responses.put_nowait(response)

    def check_status(status):
        if status in {403, 429}:
            raise ProbeError("rate_limited")
        if status == 401:
            raise ProbeError("awaiting_login")

    page.on("response", observe)
    try:
        async with asyncio.timeout(20):
            opened = await page.goto(url, wait_until="domcontentloaded", timeout=0)
            if opened:
                check_status(opened.status)
                if opened.status in {404, 410}:
                    raise ProbeError("work_unavailable")
            while True:
                response = await responses.get()
                check_status(response.status)
                if not response.ok:
                    continue
                try:
                    payload = await response.json()
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("status_code", 0) not in (0, None):
                    code = failure_code(RuntimeError(str(payload.get("status_msg", ""))))
                    if code in {"awaiting_login", "challenge", "rate_limited"}:
                        raise ProbeError(code)
                    continue
                item = at(payload, "aweme_detail")
                if isinstance(item, dict) and str(item.get("aweme_id")) == native:
                    return item
    except TimeoutError:
        raise ProbeError("douyin_detail_unavailable") from None
    finally:
        page.remove_listener("response", observe)


async def detail(client, platform, url, validator=checked_url):
    native = validator(platform, url, "work")
    if platform == "bilibili":
        data = await (client.get_video_info(aid=int(native[2:])) if native.startswith("av")
                      else client.get_video_info(bvid=native))
    elif platform == "douyin":
        from media_platform.douyin.exception import DataFetchError

        try:
            data = await client.get_video_by_id(native)
        except DataFetchError as exc:
            if failure_code(exc) != "upstream_DataFetchError":
                raise
            data = None
        if not data:
            data = await douyin_page_detail(client.playwright_page, url, native)
    else:
        from media_platform.xhs.help import parse_note_info_from_note_url

        parsed = parse_note_info_from_note_url(url)
        data = (await client.get_note_by_id(native, parsed.xsec_source, parsed.xsec_token)
                if parsed.xsec_token else None)
        if not data:
            data = await client.get_note_by_id_from_html(native, parsed.xsec_source, parsed.xsec_token, enable_cookie=True)
    if not isinstance(data, dict) or not data:
        raise ProbeError("work_unavailable")
    return data


async def resolve_short(client, platform, url, validator=checked_url):
    from tools.httpx_util import make_async_client

    validator(platform, url, "short")
    headers = {name: client.headers[name] for name in ("User-Agent", "Referer")}
    async with make_async_client(follow_redirects=False) as transport:
        for _ in range(6):
            try:
                validator(platform, url, "work")
                return url
            except ProbeError:
                try:
                    validator(platform, url, "short")
                except ProbeError:
                    raise ProbeError("short_redirect_not_work") from None
            if platform == "douyin":
                target = await client.resolve_short_url(url)
            else:
                async with transport.stream("GET", url, headers=headers, timeout=15) as response:
                    target = response.headers.get("Location") if response.is_redirect else None
            if not target:
                break
            url = urljoin(url, target)
    raise ProbeError("short_unresolved")


def douyin_video_urls(data):
    fields = ("play_addr_h264", "play_addr_256", "play_addr", "download_addr")
    video = at(data, "video")
    groups = [at(video, f"{field}.url_list") for field in fields]
    variants = at(video, "bit_rate")
    if isinstance(variants, list):
        groups.extend(at(variant, "play_addr.url_list") for variant in variants)
    urls = dict.fromkeys(url for group in groups if isinstance(group, list)
                         for url in group if isinstance(url, str) and url)
    # Try the CDN URLs before the site's redirect endpoint.
    return sorted(urls, key=lambda url: "/aweme/v1/play/" in url)


async def download(platform, client, data, destination):
    from httpx import RequestError
    from tools.httpx_util import make_async_client

    if platform == "bilibili":
        import config

        config.BILI_QN = 32
        result = await client.get_video_play_url(data["View"]["aid"], data["View"]["cid"])
        segments = result.get("durl") or []
        if len(segments) != 1:
            raise ProbeError("media_segment_layout")
        urls = [segments[0].get("url")]
        allowed = ("bilivideo.com", "bilivideo.cn", "bilibili.com", "hdslb.com")
    elif platform == "douyin":
        urls = douyin_video_urls(data)
        allowed = ("douyinvod.com", "douyin.com", "bytecdn.cn", "amemv.com", "douyinpic.com", "byteimg.com")
    else:
        from store.xhs import get_video_url_arr

        urls = get_video_url_arr(data)
        allowed = ("xhscdn.com", "xiaohongshu.com")
    # Stream through MediaCrawler's transport with the platform's media headers and a byte cap.
    # The upstream convenience clients buffer the complete download in memory.
    headers = {key: value for key, value in client.headers.items()
               if key.lower() in {"referer", "user-agent"}}
    failure = ProbeError("media_unavailable")
    async with make_async_client(follow_redirects=False) as transport:
        for url in (url for url in urls if url):
            try:
                with destination.open("wb") as output:
                    await stream_media(transport, url, allowed, headers, output)
                info, _ = audio_probe(destination)
                if not any(stream["codec_type"] == "video" for stream in info["streams"]):
                    raise ProbeError("media_has_no_audio_video")
                return info
            except ProbeError as exc:
                if exc.code == "tool_unavailable":
                    raise
                failure = ProbeError("media_invalid") if exc.code == "tool_failed" else exc
            except (RequestError, socket.gaierror) as exc:
                code = failure_code(exc)
                failure = ProbeError("network" if code.startswith("upstream_") else code)
    raise failure


async def stream_media(transport, url, allowed, headers, output, *, max_bytes=MAX_MEDIA_BYTES, timeout=30):
    for _ in range(6):
        parsed = urlsplit(url)
        host = parsed.hostname
        if (not host or parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or not any(match_host(host, domain) for domain in allowed)):
            raise ProbeError("media_host_not_supported")
        targets = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if not targets or any(not ipaddress.ip_address(address[0]).is_global for *_, address in targets):
            raise ProbeError("media_host_not_supported")
        async with transport.stream("GET", url, headers=headers, timeout=timeout) as response:
            if response.is_redirect and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                continue
            if response.status_code != 200:
                raise ProbeError(f"media_http_{response.status_code}")
            total = 0
            async for chunk in response.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ProbeError("media_size_limit")
                output.write(chunk)
            if not total:
                raise ProbeError("empty_media")
            return
    raise ProbeError("media_redirect_limit")


async def verify_original(browser, url):
    expected = checked_url(browser.platform, url, "work")
    page = await browser.context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        if not response or not response.ok:
            raise ProbeError(f"original_http_{response.status}" if response else "original_unavailable")
        native = checked_url(browser.platform, page.url, "work")
        if native != expected and not (browser.platform == "bilibili" and expected.startswith("av")):
            raise ProbeError("original_id_mismatch")
        title = await page.title()
        if not title or any(value in title for value in ("验证码", "安全验证", "访问受限", "页面不存在", "出错啦", "404")):
            raise ProbeError("original_unavailable")
        return native
    finally:
        await page.close()


async def account_details(client, platform, url):
    native = checked_url(platform, url, "account")
    if platform == "bilibili":
        raw = await client.get_creator_info(int(native))
        label = at(raw, "name")
        resolved_id = at(raw, "mid")
    elif platform == "douyin":
        raw = at(await client.get_user_info(native), "user")
        label = at(raw, "nickname")
        resolved_id = at(raw, "sec_uid")
    else:
        from media_platform.xhs.help import parse_creator_info_from_url

        parsed = parse_creator_info_from_url(url)
        raw = await client.get_creator_info(native, parsed.xsec_token, parsed.xsec_source)
        label = at(raw, "basic_info.nickname") or at(raw, "nickname")
        resolved_id = at(raw, "user_id") or at(raw, "id")
    if not isinstance(raw, dict) or not raw or not (label or resolved_id):
        raise ProbeError("account_unavailable")
    if resolved_id and str(resolved_id) != native:
        raise ProbeError("account_id_mismatch")
    return {"native_id": native, "profile_url": public_url(url), "nickname": label}


async def execute(browser, client, args, operation, candidates):
    platform = args.platform
    if operation == "account":
        account = await account_details(client, platform, args.account)
        return {"native_id": account["native_id"], "profile_url": account["profile_url"],
                "nickname_available": bool(account["nickname"])}
    if operation in {"scan", "search"}:
        rows = {}
        for raw in await list_results(client, platform, operation, args.account, args.keyword):
            try:
                row = normalize(platform, raw)
            except ProbeError:
                continue
            if row["native_id"] in rows:
                continue
            candidates.append((row["kind"], canonical_input(platform, raw, row)))
            rows[row["native_id"]] = row
        if not rows:
            raise ProbeError("no_matching_sample")
        return {"items": list(rows.values()), "metric_source": "list", "limit": LIMIT,
                **({"keyword": args.keyword} if operation == "search" else {})}
    if operation == "short":
        if not args.short_url:
            raise ProbeError("short_sample_required")
        url = await resolve_short(client, platform, args.short_url)
        return {"work": normalize(platform, await detail(client, platform, url)), "metric_source": "detail"}
    if operation == "article":
        url = args.article_url or next((url for kind, url in candidates if kind == "article"), "")
    else:
        url = args.work or next((url for kind, url in candidates if kind == "video"), "")
    if not url:
        raise ProbeError("work_sample_required")
    if operation == "original":
        return {"native_id": await verify_original(browser, url), "original_url": public_url(url)}
    raw = await detail(client, platform, url)
    work = normalize(platform, raw)
    if operation == "article" and work["kind"] != "article":
        raise ProbeError("article_sample_required")
    if operation == "article" and not work["body_available"]:
        raise ProbeError("article_body_unavailable")
    if operation == "media":
        if work["kind"] != "video":
            raise ProbeError("video_sample_required")
        directory = STATE / "media" / platform
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "sample.download"
        try:
            info = await download(platform, client, raw, temporary)
            audio = extract_audio(temporary, directory / "sample-16k.wav", max_seconds=30)
            temporary.replace(directory / "sample.media")
        finally:
            temporary.unlink(missing_ok=True)
        return {"native_id": work["native_id"], "media": info, "pcm": audio,
                "audio_file": f"media/{platform}/sample-16k.wav"}
    return {"work": work, "metric_source": "detail"}


def failure_code(exc):
    if isinstance(exc, ProbeError):
        return exc.code
    attempt = getattr(exc, "last_attempt", None)
    if attempt is not None and attempt.failed and (underlying := attempt.exception()):
        return failure_code(underlying)
    if type(exc).__name__ == "IPBlockError":
        return "rate_limited"
    if type(exc).__name__ == "NoteNotFoundError":
        return "work_unavailable"
    if type(exc).__name__ == "TargetClosedError":
        return "browser_closed"
    message = str(exc).lower()
    if "certificate_verify_failed" in message or "certificate verify failed" in message:
        return "tls_certificate"
    if any(s in message for s in ("captcha", "verifytype", "验证码", "验证后", "安全验证")):
        return "challenge"
    if any(s in message for s in ("429", "-352", "412", "风控", "访问频繁", "account blocked",
                                  "request blocked with http 403", "security limit", "security restriction", "请求被拦截")):
        return "rate_limited"
    if any(s in message for s in ("401", "-101", "未登录", "请登录", "not logged in")):
        return "awaiting_login"
    if "timeout" in message or type(exc).__name__ == "TimeoutError":
        return "timeout"
    if any(s in message for s in ("net::", "connection", "ssl", "network")):
        return "network"
    if any(s in message for s in ("profile", "singletonlock")):
        return "profile_in_use"
    return f"upstream_{type(exc).__name__}"


async def collect(playwright, args):
    pending = [args.operation] if args.operation else list(OPERATIONS)
    if not args.operation and args.platform != "xiaohongshu":
        pending.remove("article")
    name = f"{args.platform}-guest" if args.guest else args.platform
    all_passed = True
    candidates = []
    try:
        async with Browser(playwright, args.platform, guest=args.guest) as browser:
            if not args.guest:
                await wait_session(browser, 25)
            client = await browser.client()
            while pending:
                operation = pending.pop(0)
                try:
                    async with asyncio.timeout(150 if operation == "media" else 50):
                        result = await execute(browser, client, args, operation, candidates)
                    evidence(name, operation, "pass", **result)
                except Exception as exc:
                    code = failure_code(exc)
                    evidence(name, operation, "blocked" if code.endswith("_required") else "fail", error=code)
                    all_passed = False
                    if code in {"awaiting_login", "challenge", "rate_limited"}:
                        raise ProbeError(code) from None
                if pending:
                    await asyncio.sleep(2)
    except Exception as exc:
        code = failure_code(exc)
        for operation in pending:
            evidence(name, operation, "blocked", error=code)
        return False
    return all_passed


async def offline(playwright, *, report=True):
    import tempfile

    from media_platform.douyin.help import get_a_bogus_from_js

    for _, _, module, client in PLATFORMS.values():
        getattr(importlib.import_module(f"media_platform.{module}.client"), client)
    if not get_a_bogus_from_js("/aweme/v1/web/aweme/detail/", "aweme_id=1", "Mozilla/5.0"):
        raise ProbeError("node_signing_failed")
    host = "https://creator-radar.test/"
    with tempfile.TemporaryDirectory(prefix="cr2-profile-") as directory:
        for attempt in range(2):
            context = await playwright.chromium.launch_persistent_context(directory, headless=True)
            try:
                page = context.pages[0]
                await page.route(host, lambda route: route.fulfill(status=200, body="offline validation"))
                await page.goto(host)
                if not attempt:
                    await page.evaluate("localStorage.setItem('cr2', 'ok')")
                elif await page.evaluate("localStorage.getItem('cr2')") != "ok":
                    raise ProbeError("profile_not_persistent")
            finally:
                await context.close()
    result = {"revision": REVISION, "python": sys.version.split()[0],
              "playwright": importlib.metadata.version("playwright"),
              "node": run_tool(["node", "--version"], 10).decode().strip(),
              "chromium": Path(playwright.chromium.executable_path).parent.parent.name}
    if report:
        evidence("runtime", "crawler", "pass", **result)
    return result


async def main(args):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        if args.command == "offline":
            await offline(playwright)
        elif args.command == "login":
            return await login(playwright, args)
        else:
            return await collect(playwright, args)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("offline")
    login_cli = sub.add_parser("login")
    login_cli.add_argument("platform", choices=PLATFORMS)
    login_cli.add_argument("--wait", type=int, default=900)
    collect_cli = sub.add_parser("collect")
    collect_cli.add_argument("platform", choices=PLATFORMS)
    collect_cli.add_argument("--operation", choices=OPERATIONS)
    collect_cli.add_argument("--account")
    collect_cli.add_argument("--work")
    collect_cli.add_argument("--short-url")
    collect_cli.add_argument("--article-url")
    collect_cli.add_argument("--keyword")
    collect_cli.add_argument("--samples", type=Path, help="Local JSON sample file with a section per platform")
    collect_cli.add_argument("--guest", action="store_true", help="Save unauthenticated diagnostics separately")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    requested_operation = "login" if args.command == "login" else "crawler" if args.command == "offline" else "collect"
    try:
        if args.command == "collect":
            options = {}
            if args.samples:
                try:
                    options = json.loads(args.samples.read_text(encoding="utf-8-sig"))[args.platform]
                    if not isinstance(options, dict) or any(not isinstance(value, str) for value in options.values()):
                        raise ValueError
                except (OSError, ValueError, KeyError, TypeError):
                    raise ProbeError("invalid_sample_file") from None
            account, work, short = DEFAULTS[args.platform]
            for name, default in {"account": account, "work": work, "short_url": short,
                                  "article_url": "", "keyword": "桌面收纳"}.items():
                setattr(args, name, getattr(args, name) or options.get(name) or default)
        os.chdir(args.runtime.resolve())
        validate_runtime(Path.cwd())
        if sys.version_info[:2] != (3, 12):
            raise ProbeError("python_version_mismatch")
        sys.path.insert(0, str(Path.cwd()))
        import config

        config.ENABLE_GET_COMMENTS = False
        config.ENABLE_GET_SUB_COMMENTS = False
        config.MAX_CONCURRENCY_NUM = 1
        passed = asyncio.run(main(args))
    except Exception as exc:
        code = failure_code(exc)
        name = getattr(args, "platform", "runtime")
        evidence(name, requested_operation, "blocked" if code == "awaiting_login" else "fail", error=code)
        passed = False
    sys.exit(0 if passed else 1)
