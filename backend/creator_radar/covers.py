from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

MAX_ARTICLE_IMAGES = 32
XHS_IMAGE_DOMAINS = ("xhscdn.com", "xiaohongshu.com")


def xhs_image_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme in {"http", "https"} and parsed.port in {None, 80, 443}
            and parsed.username is None and parsed.password is None and parsed.path
            and any(host == domain or host.endswith("." + domain) for domain in XHS_IMAGE_DOMAINS)
        ):
            return parsed._replace(query="", fragment="").geturl()
    except ValueError:
        pass
    return None


def path_for(workspace: Path, platform: str, native_id: str, image_url: str | None = None) -> Path:
    identity = f"{platform}:{native_id}"
    if image_url is not None:
        identity += f":{image_url}"
    key = sha256(identity.encode()).hexdigest()
    return workspace / "covers" / f"{key}.webp"


def cached(workspace: Path, platform: str, native_id: str, image_url: str | None = None):
    path = path_for(workspace, platform, native_id, image_url)
    try:
        if path.resolve() == path and path.is_file():
            return path
    except (OSError, RuntimeError):
        pass
    return None
