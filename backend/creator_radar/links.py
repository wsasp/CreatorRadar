import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

HOSTS = {
    "bilibili": ({"www.bilibili.com", "m.bilibili.com", "bilibili.com"}, {"b23.tv"}),
    "douyin": (
        {"www.douyin.com", "douyin.com", "www.iesdouyin.com", "m.iesdouyin.com"},
        {"v.douyin.com"},
    ),
    "xiaohongshu": (
        {"www.xiaohongshu.com", "xiaohongshu.com", "m.xiaohongshu.com"},
        {"xhslink.com", "www.xhslink.com"},
    ),
}
WORK = {
    "bilibili": r"/video/(BV[0-9A-Za-z]+|av[0-9]+)/?",
    "douyin": r"/(?:share/)?(?:video|note)/([0-9]+)/?",
    "xiaohongshu": r"/(?:explore|item|discovery/item)/([0-9a-f]{24})/?",
}
PROFILE = {
    "bilibili": r"/([0-9]+)(?:/.*)?",
    "douyin": r"/user/([A-Za-z0-9_-]+)/?",
    "xiaohongshu": r"/user/profile/([0-9a-f]{24})/?",
}
SHORT = {
    "bilibili": r"/[A-Za-z0-9_-]+/?",
    "douyin": r"/[A-Za-z0-9_-]+/?",
    "xiaohongshu": r"/(?:[a-z]/)?[A-Za-z0-9_-]+/?",
}


class LinkError(ValueError):
    pass


@dataclass(frozen=True)
class PlatformLink:
    platform: str
    kind: str
    native_id: str | None
    url: str
    public_url: str


def parse_link(value: str) -> PlatformLink:
    value = value.strip()
    if not value or len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise LinkError("invalid_url")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise LinkError("invalid_url") from None
    if (
        url.scheme not in {"http", "https"}
        or url.username is not None
        or url.password is not None
        or port not in {None, 80 if url.scheme == "http" else 443}
    ):
        raise LinkError("invalid_url")
    host = url.hostname
    platform = next(
        (
            name
            for name, (hosts, shorts) in HOSTS.items()
            if host in hosts | shorts or (name == "bilibili" and host == "space.bilibili.com")
        ),
        None,
    )
    if not platform:
        raise LinkError("unsupported_platform")
    kind = "short" if host in HOSTS[platform][1] else ""
    native = None
    path = url.path.rstrip("/")
    if kind:
        if not re.fullmatch(SHORT[platform], url.path):
            raise LinkError("invalid_link_type")
    elif host != "space.bilibili.com" and (match := re.fullmatch(WORK[platform], url.path)):
        kind, native = "work", match[1]
    elif (platform != "bilibili" or host == "space.bilibili.com") and (
        match := re.fullmatch(PROFILE[platform], url.path)
    ):
        kind, native = "account", match[1]
        if platform == "bilibili":
            path = "/" + native
    else:
        raise LinkError("invalid_link_type")
    public = urlunsplit((url.scheme, host, path, "", ""))
    if kind == "short":
        query = url.query
    elif platform == "xiaohongshu":
        params = parse_qs(url.query)
        query = urlencode(
            {key: params[key][0] for key in ("xsec_token", "xsec_source") if key in params}
        )
    else:
        query = ""
    return PlatformLink(
        platform, kind, native, urlunsplit((url.scheme, host, path, query, "")), public
    )
