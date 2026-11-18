import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from common import ProbeError
from crawler import stream_media
from creator_radar.covers import XHS_IMAGE_DOMAINS, cached, path_for

CDN_DOMAINS = {
    "bilibili": ("hdslb.com", "bilivideo.com", "bilivideo.cn"),
    "douyin": ("douyinpic.com", "byteimg.com", "douyinvod.com"),
    "xiaohongshu": XHS_IMAGE_DOMAINS,
}
MAX_COVER_BYTES = 5 * 1024 * 1024
MAX_ARTICLE_IMAGE_BYTES = 12 * 1024 * 1024


def save_cover(data, path, *, detail=False):
    from PIL import Image, ImageOps

    with Image.open(data, formats=("JPEG", "PNG", "WEBP", "GIF", "AVIF")) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Cover dimensions exceed limit")
        image = ImageOps.exif_transpose(image)
        edge = 2560 if detail else 960
        image.thumbnail((edge, edge))
        if path.parent.resolve() != path.parent:
            raise OSError("Invalid cover directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, suffix=".partial", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            image.save(temporary_path, format="WEBP", quality=84)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


async def cache_covers(workspace, platform, client, rows):
    from httpx import HTTPError
    from PIL import Image
    from tools.httpx_util import make_async_client

    pending = []
    remaining_images = []
    for row in rows:
        urls = row.pop("cover_candidates")
        native_id = row["native_id"]
        images = row.pop("image_candidates", {})
        if images:
            row["image_urls"] = list(images)
            for index, (identity, variants) in enumerate(images.items()):
                if not cached(workspace, platform, native_id, identity):
                    queue = pending if index == 0 else remaining_images
                    queue.append((path_for(workspace, platform, native_id, identity), variants, True))
        elif urls and not cached(workspace, platform, native_id):
            pending.append((path_for(workspace, platform, native_id), urls, False))
    pending.extend(remaining_images)
    if not pending:
        return

    headers = {key: value for key, value in client.headers.items()
               if key.lower() in {"referer", "user-agent"}}
    slots = asyncio.Semaphore(6)

    async def cache_one(transport, path, urls, detail):
        limit = MAX_ARTICLE_IMAGE_BYTES if detail else MAX_COVER_BYTES
        async with slots:
            for url in urls:
                try:
                    async with asyncio.timeout(5):
                        data = BytesIO()
                        await stream_media(transport, url, CDN_DOMAINS[platform], headers, data,
                                           max_bytes=limit, timeout=4)
                    data.seek(0)
                    await asyncio.to_thread(save_cover, data, path, detail=detail)
                    return
                except (
                    ProbeError, HTTPError, OSError, ValueError, SyntaxError,
                    Image.DecompressionBombError,
                ):
                    continue

    try:
        async with asyncio.timeout(24), make_async_client(follow_redirects=False) as transport:
            await asyncio.gather(*(cache_one(transport, *item) for item in pending))
    except TimeoutError:
        pass
