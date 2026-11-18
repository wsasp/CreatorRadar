import asyncio
import errno
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "integrations"))
import asr_bridge  # noqa: E402
from bridge import collect, work_row  # noqa: E402
from common import ProbeError  # noqa: E402
from crawler import checked_url, douyin_page_detail, douyin_video_urls, normalize, public_url  # noqa: E402
from processing import validate_analysis, validate_segments  # noqa: E402
from runtime_guard import MANIFEST, file_hashes, validate_runtime  # noqa: E402


def test_packaged_crawler_rejects_changes_to_verified_source(tmp_path, monkeypatch):
    from common import REVISION

    monkeypatch.setenv("CREATOR_RADAR_BUNDLED", "1")
    source = tmp_path / "client.py"
    source.write_text("# packaged source", encoding="utf-8")
    (tmp_path / MANIFEST).write_text(json.dumps({
        "revision": REVISION, "files": file_hashes(tmp_path),
    }), encoding="utf-8")
    validate_runtime(tmp_path)
    source.write_text("# changed source", encoding="utf-8")
    with pytest.raises(ProbeError, match="runtime_modified"):
        validate_runtime(tmp_path)


def test_whisper_download_uses_project_cache_and_reuses_system_cache(tmp_path, monkeypatch):
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(asr_bridge, "WHISPER_CACHE", tmp_path)
    cache = tmp_path / "hub"
    cache.mkdir()
    calls = []

    def model(name, **settings):
        calls.append((name, settings.get("download_root"), settings["local_files_only"]))
        if settings["local_files_only"] and settings.get("download_root"):
            raise FileNotFoundError
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=model))
    asr_bridge.load_model("faster-whisper", "large-v3-turbo", local_only=True)
    asr_bridge.load_model("faster-whisper", "large-v3-turbo", local_only=False)
    asr_bridge.load_model("faster-whisper", "small", local_only=False)
    assert calls == [
        ("large-v3-turbo", str(cache), True), ("large-v3-turbo", None, True),
        ("large-v3-turbo", str(cache), False), ("small", None, False),
    ]
    assert asr_bridge.model_disk_full(OSError(errno.ENOSPC, "disk full"))
    assert asr_bridge.model_disk_full(RuntimeError("write failed (os error 112)"))
    assert not asr_bridge.model_disk_full(RuntimeError("download timed out"))


def test_xhs_zero_missing_and_reads_keep_their_meaning():
    row = normalize("xiaohongshu", {"id": "64b95d01000000000c034587", "note_card": {
        "type": "normal", "interactInfo": {"likedCount": "0", "viewCount": "30", "shareCount": "1万"},
    }, "xsec_token": "credential"})
    assert row["metrics"]["plays"]["availability"] == "not_applicable"
    assert row["metrics"]["likes"] == {"field": "note_card.interactInfo.likedCount", "count": 0,
                                       "availability": "available"}
    assert row["metrics"]["comments"]["availability"] == "missing"
    assert row["metrics"]["shares"]["availability"] == "unparsed"
    assert row["reads"]["count"] == 30
    assert "credential" not in json.dumps(row)


def test_bili_search_danmaku_is_not_comment_count():
    row = normalize("bilibili", {"bvid": "BV123", "play": 300, "video_review": 15})
    assert row["metrics"]["plays"]["count"] == 300
    assert row["metrics"]["comments"]["count"] is None


def test_public_url_strips_query_and_fragment():
    url = "https://www.xiaohongshu.com/explore/64b95d01000000000c034587"
    assert public_url(url + "?xsec_token=secret#hidden") == url


@pytest.mark.parametrize("url", [
    "https://b23.tv.other.test/abc", "https://other.test/b23.tv", "http://b23.tv:80@other.test/abc",
    "https://b23.tv:8888/abc",
])
def test_short_url_rejects_wrong_destination(url):
    with pytest.raises(ProbeError):
        checked_url("bilibili", url, "short")


def analysis_example(ref):
    point = {"text": "例子", "refs": [ref]}
    return {"summary": point, "hook": point, "structure": [point], "key_points": [point],
            "techniques": [point], "extensions": [point]}


def test_analysis_invalid_json_and_out_of_bounds_reference():
    assert validate_analysis(json.dumps(analysis_example(1)), [{}])["summary"]["refs"] == [1]
    with pytest.raises(ProbeError, match="reference_out_of_bounds"):
        validate_analysis(json.dumps(analysis_example(2)), [{}])
    with pytest.raises(ProbeError, match="invalid_json_or_schema"):
        validate_analysis("{", [{}])


def test_asr_rejects_timestamp_beyond_audio():
    with pytest.raises(ProbeError, match="invalid_asr_segments"):
        validate_segments([{"start": 0, "end": 4.2, "text": "早上好"}], 4)


def test_bridge_scan_uses_cursor_cutoff_and_keeps_partial_rows(monkeypatch):
    import bridge

    calls = []

    async def page(_client, _platform, _request, index, cursor, search_id):
        calls.append((index, cursor))
        if index == 2:
            raise ProbeError("rate_limited")
        return [
            {"aweme_id": "10", "create_time": 100, "video": {},
             "statistics": {"play_count": 0}},
            {"aweme_id": "9", "create_time": 10, "video": {}},
            {"aweme_id": "10", "create_time": 100, "video": {}},
        ], 15, search_id

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr(bridge, "page", page)
    monkeypatch.setattr(bridge.asyncio, "sleep", no_delay)
    result = asyncio.run(collect(None, "douyin", {"limit": 5, "cutoff_seconds": 50}))
    assert calls == [(1, ""), (2, 15)]
    assert result["partial_error"] == "rate_limited"
    assert [item["native_id"] for item in result["items"]] == ["10"]
    assert result["items"][0]["metrics"]["plays"]["count"] == 0


def test_douyin_media_prefers_direct_candidates_even_with_one_url():
    direct = "https://v26-web.douyinvod.com/example.mp4"
    alternate = "https://v11-weba.douyinvod.com/example.mp4"
    redirect = "https://www.douyin.com/aweme/v1/play/?video_id=123"
    assert douyin_video_urls({"video": {"play_addr_h264": {"url_list": [direct]}}}) == [direct]
    assert douyin_video_urls({"video": {
        "play_addr_h264": {"url_list": [redirect]},
        "play_addr": {"url_list": [direct, direct]},
        "bit_rate": [{"play_addr": {"url_list": [alternate, redirect]}}],
    }}) == [direct, alternate, redirect]


def test_douyin_cover_candidates_keep_the_original_signature_for_download():
    signed = "https://p3-pc-sign.douyinpic.com/cover.jpeg?ts=123&sig=encoded%2Bvalue"
    alternate = "https://p9-pc-sign.douyinpic.com/cover.jpeg?sig=other"
    row = work_row("douyin", {"aweme_id": "123", "video": {
        "cover": {"url_list": [signed, signed]},
        "origin_cover": {"url_list": [alternate]},
    }})
    assert row["cover_candidates"] == [signed, alternate]
    assert row["cover_url"] == signed.split("?", 1)[0]


def test_xhs_gallery_keeps_note_order_and_groups_alternate_urls():
    first = "https://sns-webpic.xhscdn.com/first"
    second = "https://sns-webpic.xhscdn.com/second"
    row = work_row("xiaohongshu", {
        "id": "0123456789abcdef01234567", "type": "normal",
        "imageList": [
            {"urlDefault": first + "?signature=image-one", "urlPre": first + "?preview=1"},
            {"infoList": [{"url": "https://external.example/invalid"}, {"url": second}]},
            {"urlDefault": first + "?signature=duplicate"},
        ],
    }, with_gallery=True)
    assert list(row["image_candidates"]) == [first, second]
    assert row["image_candidates"][first] == [
        first + "?signature=image-one", first + "?preview=1", first + "?signature=duplicate",
    ]
    assert row["image_candidates"][second] == [second]


def test_douyin_page_detail_matches_native_id():
    url = "https://www.douyin.com/video/123"
    endpoint = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id="
    item = {"aweme_id": "123", "video": {"duration": 5000}}

    async def response_json():
        return {"aweme_detail": item}

    async def goto(target, **_options):
        assert target == url

    @asynccontextmanager
    async def expect_response(predicate, **_options):
        assert not predicate(SimpleNamespace(url=endpoint + "456"))
        assert not predicate(SimpleNamespace(url=endpoint.replace("douyin.com", "example.com") + "123"))
        assert predicate(SimpleNamespace(url=endpoint + "123"))

        async def response():
            return SimpleNamespace(status=200, ok=True, json=response_json)

        yield SimpleNamespace(value=response())

    page = SimpleNamespace(goto=goto, expect_response=expect_response)
    assert asyncio.run(douyin_page_detail(page, url, "123")) == item
    item["aweme_id"] = "456"
    with pytest.raises(ProbeError, match="douyin_detail_unavailable"):
        asyncio.run(douyin_page_detail(page, url, "123"))
