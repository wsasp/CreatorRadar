import json
import shutil
import wave
from pathlib import Path
from threading import Event

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from creator_radar import analysis_client as ai
from creator_radar import codex_client, collector, funasr_client, media, processing, queue, worker
from creator_radar.api import create_app
from creator_radar.config import ROOT, Config
from creator_radar.db import make_engine, migrate, write_transaction
from creator_radar.schema import analyses, events, media_cache, topics, transcriptions, works

PREFIX = "/api/v1/"
VIDEO = "https://www.bilibili.com/video/BV1Fixture01"
ARTICLE = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
BASE = "http://127.0.0.1:10999/v1"
KEY = "private-test-key"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = Config(workspace="acceptance", data_dir=tmp_path, crawler_fixture=True)
    migrate(config)
    engine = make_engine(config)
    secrets = {}

    def write_key(config, base, value):
        secrets[(config.workspace_dir, base)] = value

    def read_key(config, base):
        return secrets.get((config.workspace_dir, base))

    monkeypatch.setattr(ai, "read_key", read_key)
    monkeypatch.setattr(ai, "write_key", write_key)
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        yield config, engine, client
    engine.dispose()


def get(client, path):
    response = client.get(PREFIX + path)
    assert response.is_success, response.text
    return response.json()


def post(client, path, body=None):
    response = client.post(PREFIX + path, json=body)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def execute(workspace):
    config, engine, _ = workspace
    job = queue.claim(engine, "worker")
    assert job is not None
    worker.execute(engine, config, job, Event())
    return get(workspace[2], "jobs/" + job["id"])


def capture(workspace, url=VIDEO):
    post(workspace[2], "quick-captures", {"url": url})
    return execute(workspace)["result"]


def configure_ai(client, base=BASE):
    assert client.patch(PREFIX + "settings", json={
        "ai": {"base_url": base, "model": "test-model"},
    }).is_success
    response = client.put(PREFIX + "settings/ai-key", json={"api_key": KEY})
    assert response.is_success, response.text
    assert response.json()["ai"]["key_configured"] is True
    assert KEY not in response.text


def transcript(_config, request, keep_running):
    assert keep_running()
    return {
        "engine": request["engine"], "model": request["model"], "model_revision": None,
        "language": "zh", "audio_sha256": "1" * 64,
        "segments": [{"start": 0, "end": 1, "text": "一段轉寫。"}],
    }


def seed_audio(config, engine, work_id):
    target = config.workspace_dir / "media" / "bilibili" / ("0" * 32 + "-1.wav")
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 16_000)
    with write_transaction(engine) as connection:
        media.record(connection, config, work_id, "audio", target, queue.now_ms())
    return target


def test_remote_funasr_settings_and_transcription(workspace, monkeypatch):
    config, engine, client = workspace
    secrets = {}
    monkeypatch.setattr(funasr_client, "service_key", lambda config, base: secrets.get(base))

    def save_key(_config, base, key):
        secrets[base] = key

    monkeypatch.setattr(funasr_client, "save_key", save_key)
    asr = {"engine": "FunASR", "model": "paraformer", "language": "zh", "base_url": BASE}
    missing = client.patch(PREFIX + "settings", json={"asr": asr})
    assert missing.json()["code"] == "asr_api_key_required"
    settings = client.patch(PREFIX + "settings", json={"asr": {
        **asr, "base_url": BASE + "/audio/transcriptions", "api_key": KEY,
    }})
    assert settings.json()["asr"] == {**asr, "key_configured": True}
    assert KEY not in settings.text

    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {KEY}"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "paraformer"}]})
        assert request.url.path == "/v1/audio/transcriptions"
        assert b'name="file"; filename="audio.wav"' in request.content
        assert b"RIFF" in request.content and b"verbose_json" in request.content
        return httpx.Response(200, json={"segments": [{"start": 0, "end": 1, "text": "測試。"}]})

    def connect(**options):
        return original(transport=httpx.MockTransport(handler), **options)

    monkeypatch.setattr(funasr_client.httpx, "AsyncClient", connect)
    check = client.post(PREFIX + "settings/asr/check").json()
    assert check["engine"] == check["model"] == "ready"
    captured = capture(workspace)
    seed_audio(config, engine, captured["work_id"])
    topic = f"topics/{captured['topic_id']}"
    post(client, f"{topic}/transcriptions")
    job = execute(workspace)
    assert job["status"] == "succeeded"
    assert KEY not in json.dumps(job)
    result = get(client, topic)["transcription"]
    assert result["engine"] == "FunASR"
    assert result["segments"] == [{"start": 0, "end": 1, "text": "测试。"}]

    post(client, f"{topic}/transcriptions")
    response = client.patch(PREFIX + "settings", json={"asr": {
        **asr, "base_url": "https://changed.example/v1", "api_key": "another-key",
    }})
    assert response.is_success
    assert execute(workspace)["error_code"] == "asr_api_settings_changed"
    assert requests == ["/v1/models", "/v1/audio/transcriptions"]


def example(ref=1):
    point = {"text": "示例拆解", "refs": [ref]}
    return {
        "summary": point, "hook": point, "structure": [point], "key_points": [point],
        "techniques": [point], "extensions": [{"text": "证据不足", "refs": []}],
    }


def mock_endpoint(monkeypatch, reply):
    original = httpx.AsyncClient

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        return reply(json.loads(payload["messages"][1]["content"]))

    def connect(**options):
        return original(transport=httpx.MockTransport(handler), **options)

    monkeypatch.setattr(ai.httpx, "AsyncClient", connect)


def ai_response(ref=1):
    return httpx.Response(200, json={"choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps(example(ref))},
    }]})


def test_codex_analysis_uses_saved_options_and_preserves_api_settings(workspace, monkeypatch):
    _, _, client = workspace
    topic_id = capture(workspace, ARTICLE)["topic_id"]
    endpoint = f"topics/{topic_id}/analyses"
    configure_ai(client)

    calls = []

    def run(command, payload, timeout, keep_running, *, cwd, env):
        model = next(arg for arg in command if arg.startswith("--model="))
        effort = next(arg for arg in command if arg.startswith("model_reasoning_effort="))
        calls.append((model, effort))
        assert keep_running()
        assert command[-1] == "-" and "sources" in payload.decode()
        schema_path = Path(command[command.index("--output-schema") + 1])
        assert json.loads(schema_path.read_text(encoding="utf-8"))["title"] == "Analysis"
        output = Path(command[command.index("-o") + 1])
        output.write_text(json.dumps(example()), encoding="utf-8")
        return 0, b""

    monkeypatch.setattr(codex_client, "executable", lambda: ["codex"])
    monkeypatch.setattr(collector, "communicate", run)
    selected = client.patch(PREFIX + "settings", json={"ai": {
        "provider": "codex_cli", "codex_model": "test-codex", "codex_reasoning_effort": "high",
    }})
    assert selected.is_success and selected.json()["ai"]["key_configured"]
    post(client, endpoint)
    assert client.patch(PREFIX + "settings", json={
        "ai": {"codex_reasoning_effort": "medium"},
    }).is_success
    assert execute(workspace)["error_code"] == "ai_settings_changed"
    assert calls == []

    post(client, endpoint)
    assert execute(workspace)["status"] == "succeeded"
    assert get(client, f"topics/{topic_id}")["analysis"]["model"] == "test-codex"
    assert client.post(PREFIX + "settings/ai/test").json() == {"status": "ready"}
    assert calls == [("--model=test-codex", 'model_reasoning_effort="medium"')] * 2

    restored = client.patch(PREFIX + "settings", json={"ai": {"provider": "api"}}).json()["ai"]
    assert (restored["base_url"], restored["model"], restored["key_configured"]) == (
        BASE, "test-model", True,
    )
    assert (restored["codex_model"], restored["codex_reasoning_effort"]) == ("test-codex", "medium")


def test_migrating_phase3_keeps_topics_and_imports_completed_media(tmp_path):
    config = Config(workspace="acceptance", data_dir=tmp_path)
    config.workspace_dir.mkdir()
    engine = make_engine(config)
    alembic = AlembicConfig()
    alembic.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    work_id, topic_id = "1" * 32, "2" * 32
    with write_transaction(engine) as connection:
        alembic.attributes["connection"] = connection
        command.upgrade(alembic, "0002_discovery")
        connection.exec_driver_sql(
            "INSERT INTO works(id, platform, native_id, kind, title, original_url, collector_url, "
            "created_at, collected_at, metrics) VALUES (?, 'bilibili', 'BV1Fixture01', 'video', "
            "'测试', ?, ?, 1, 1, '{}')",
            (work_id, VIDEO, VIDEO),
        )
        connection.exec_driver_sql(
            "INSERT INTO topics(id, work_id, source, snapshot, archived, created_at) "
            "VALUES (?, ?, '{}', '{}', 1, 1)",
            (topic_id, work_id),
        )
    job_id = queue.submit(engine, queue.Submission("media_fetch", work_id, "测试", "bilibili"))
    job = queue.claim(engine, "old-worker")
    path = f"media/bilibili/{job_id}-1.media"
    queue.finish(engine, job, "succeeded", "完成", result={"work_id": work_id, "media_path": path})
    migrate(config)
    with engine.connect() as connection:
        topic = connection.execute(select(topics)).mappings().one()
        assert topic["id"] == topic_id and topic["archived"]
        assert topic["transcription_id"] is None and topic["analysis_id"] is None
        assert connection.scalar(select(media_cache.c.media_path)) == path
        assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()


@pytest.mark.parametrize("source", ["scan", "search", "quick"])
def test_transcription_from_each_adoption_entry(workspace, monkeypatch, source):
    config, engine, client = workspace
    if source == "quick":
        captured = capture(workspace)
        topic_id, work_id = captured["topic_id"], captured["work_id"]
    else:
        if source == "scan":
            post(client, "accounts", {"url": "https://space.bilibili.com/434377496"})
            source_id = execute(workspace)["result"]["account_id"]
            post(client, "account-scans", {"account_ids": [source_id]})
            execute(workspace)
            endpoint = "feed"
        else:
            response = client.post(PREFIX + "searches", json={
                "keyword": "桌面", "platforms": ["bilibili"],
            })
            assert response.status_code == 202
            source_id = response.json()["search_id"]
            execute(workspace)
            endpoint = f"searches/{source_id}/results"
        work_id = get(client, endpoint)["items"][0]["id"]
        adopted = client.post(PREFIX + "topics", json={
            "work_id": work_id, "kind": source, "source_id": source_id,
        })
        assert adopted.is_success
        topic_id = adopted.json()["topic_id"]

    seed_audio(config, engine, work_id)
    monkeypatch.setattr(processing, "run_asr", transcript)
    endpoint = f"topics/{topic_id}"
    post(client, endpoint + "/transcriptions")
    assert execute(workspace)["status"] == "succeeded"
    assert capture(workspace)["topic_id"] == topic_id
    topic = get(client, endpoint)
    assert topic["source"]["kind"] == source
    assert topic["transcription"]["segments"]


def test_transcribe_retry_uses_audio_and_preserves_success(workspace, monkeypatch):
    config, engine, client = workspace
    item = capture(workspace)
    seed_audio(config, engine, item["work_id"])
    path = "topics/" + item["topic_id"]
    endpoint = path + "/transcriptions"
    article = capture(workspace, ARTICLE)
    article_asr = PREFIX + "topics/" + article["topic_id"] + "/transcriptions"
    assert client.post(article_asr).status_code == 409
    assert client.post(PREFIX + "topics/" + item["work_id"] + "/transcriptions").status_code == 404
    assert client.post(PREFIX + path + "/analyses").json()["code"] == "transcript_required"

    def no_download(*_args):
        pytest.fail("Cached PCM must be reused")

    monkeypatch.setattr(collector, "run", no_download)
    monkeypatch.setattr(processing, "run_asr", transcript)
    job_id = post(client, endpoint)
    assert post(client, endpoint) == job_id
    assert execute(workspace)["status"] == "succeeded"
    original = get(client, path)["transcription"]
    assert original["segments"] == [{"start": 0, "end": 1, "text": "一段转写。"}]
    assert get(client, "overview")["transcription"] == {"completed": 1, "total": 1}
    phase_events = get(client, f"events?job_id={job_id}")["items"]
    assert {"media", "audio_extract", "transcribing"} <= {row["code"] for row in phase_events}

    def no_speech(*_args):
        raise ai.ProcessingError("no_speech")

    monkeypatch.setattr(processing, "run_asr", no_speech)
    failed_id = post(client, endpoint)
    failed = execute(workspace)
    assert (failed["status"], failed["error_code"], failed["phase"]) == (
        "failed", "no_speech", "transcribing",
    )
    assert get(client, path)["transcription"] == original
    assert get(client, path)["processing_jobs"]["asr"]["id"] == failed_id
    monkeypatch.setattr(processing, "run_asr", transcript)
    post(client, f"jobs/{failed_id}/retry")
    assert execute(workspace)["attempt_count"] == 2
    assert get(client, path)["transcription"]["id"] != original["id"]


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg")
def test_media_is_reused_after_failed_extraction(workspace, monkeypatch, tmp_path):
    config, engine, client = workspace
    item = capture(workspace)
    sample = tmp_path / "sample.wav"
    with wave.open(str(sample), "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 16_000)
    original_communicate = collector.communicate
    downloads = 0
    fail_conversion = True

    def fetch(_config, request, _keep_running):
        nonlocal downloads
        assert request["operation"] == "media"
        downloads += 1
        shutil.copyfile(sample, request["target"])
        return {"has_audio": True}

    def communicate(command, *args):
        if "-ar" in command and fail_conversion:
            raise collector.CollectionError("timeout")
        return original_communicate(command, *args)

    monkeypatch.setattr(collector, "run", fetch)
    monkeypatch.setattr(collector, "communicate", communicate)
    monkeypatch.setattr(processing, "run_asr", transcript)
    endpoint = "topics/" + item["topic_id"] + "/transcriptions"
    job_id = post(client, endpoint)
    assert execute(workspace)["error_code"] == "audio_timeout"
    fail_conversion = False
    post(client, f"jobs/{job_id}/retry")
    assert execute(workspace)["status"] == "succeeded"
    assert downloads == 1
    with engine.connect() as connection:
        cached_video, cached_audio = media.load(connection, config, item["work_id"], 3)
        assert cached_video.is_file() and cached_audio.is_file()


def test_stale_analysis_is_saved_without_promoting_and_retry_retains_result(workspace, monkeypatch):
    config, engine, client = workspace
    item = capture(workspace)
    seed_audio(config, engine, item["work_id"])
    path = "topics/" + item["topic_id"]
    configure_ai(client)
    monkeypatch.setattr(processing, "run_asr", transcript)

    ref = 1

    def video_reply(snapshot):
        assert snapshot["kind"] == "video"
        assert snapshot["sources"][0] == {"id": 1, "start": 0, "end": 1, "text": "一段转写。"}
        assert snapshot["title"] and snapshot["description"]
        return ai_response(ref)

    mock_endpoint(monkeypatch, video_reply)
    post(client, path + "/transcriptions")
    execute(workspace)
    post(client, path + "/analyses")
    assert execute(workspace)["result"]["current"] is True
    original = get(client, path)["analysis"]
    post(client, path + "/analyses")
    running = queue.claim(engine, "analysis-worker")
    assert running["kind"] == "analysis"
    post(client, path + "/transcriptions")
    assert execute(workspace)["status"] == "succeeded"
    worker.execute(engine, config, running, Event())
    assert get(client, "jobs/" + running["id"])["result"]["current"] is False
    assert get(client, path)["analysis"] == {**original, "stale": True}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(analyses)) == 2
        assert connection.scalar(select(func.count()).select_from(transcriptions)) == 2
    ref = 2
    post(client, path + "/analyses")
    assert execute(workspace)["error_code"] == "ai_invalid_reference"
    assert get(client, path)["analysis"] == {**original, "stale": True}
    assert KEY not in json.dumps(get(client, "events"))


def test_article_analysis_validates_references_and_body_version(workspace, monkeypatch):
    _, engine, client = workspace
    item = capture(workspace, ARTICLE)
    path = "topics/" + item["topic_id"]
    configure_ai(client)
    ref = 2

    def article_reply(snapshot):
        assert snapshot["kind"] == "article"
        assert snapshot["sources"] == [{"id": 1, "text": "记录桌面收纳和创作流程。"}]
        return ai_response(ref)

    mock_endpoint(monkeypatch, article_reply)
    post(client, path + "/analyses")
    assert execute(workspace)["error_code"] == "ai_invalid_reference"
    assert get(client, path)["analysis"] is None
    ref = 1
    post(client, path + "/analyses")
    assert execute(workspace)["status"] == "succeeded"
    assert get(client, path)["analysis"]["content"]["extensions"][0]["refs"] == []
    with write_transaction(engine) as connection:
        connection.execute(works.update().where(
            works.c.id == item["work_id"],
        ).values(body="新的正文。"))
    assert get(client, path)["analysis"]["stale"] is True


def test_cancel_or_lease_loss_during_inference_cannot_write(workspace, monkeypatch):
    config, engine, client = workspace
    item = capture(workspace)
    seed_audio(config, engine, item["work_id"])
    path = "topics/" + item["topic_id"]
    job_id = post(client, path + "/transcriptions")

    def cancel(*args):
        queue.cancel(engine, job_id)
        return transcript(*args[:2], lambda: True)

    monkeypatch.setattr(processing, "run_asr", cancel)
    assert execute(workspace)["status"] == "cancelled"
    assert get(client, path)["transcription"] is None
    job_id = post(client, path + "/transcriptions")

    def expire(*args):
        clock = queue.now_ms()
        monkeypatch.setattr(queue, "now_ms", lambda: clock + 2 * queue.LEASE_MS)
        queue.recover(engine)
        return transcript(*args[:2], lambda: True)

    monkeypatch.setattr(processing, "run_asr", expire)
    assert execute(workspace)["status"] == "interrupted"
    assert get(client, path)["transcription"] is None
    assert get(client, path)["processing_jobs"]["asr"]["id"] == job_id


def test_settings_key_binding_persistence_and_cleanup(workspace, monkeypatch):
    config, engine, client = workspace
    item = capture(workspace)
    audio = seed_audio(config, engine, item["work_id"])
    monkeypatch.setattr(processing, "run_asr", transcript)
    post(client, "topics/" + item["topic_id"] + "/transcriptions")
    execute(workspace)
    configure_ai(client)
    assert client.patch(PREFIX + "settings", json={
        "asr": {"engine": "faster-whisper", "model": "small", "language": "auto"},
        "retention": {"media_days": 1, "log_days": 1},
    }).is_success
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as restarted:
        settings = get(restarted, "settings")
        assert settings["asr"]["language"] == "auto"
        assert settings["ai"]["key_configured"]
        assert settings["retention"] == {"media_days": 1, "log_days": 1, "search_days": 14}
    assert client.patch(PREFIX + "settings", json={
        "ai": {"base_url": "https://different.example", "model": "test-model"},
    }).json()["ai"]["key_configured"] is False
    conflict = client.put(PREFIX + "settings/ai-key", json={"api_key": KEY, "base_url": BASE})
    assert conflict.status_code == 409 and KEY not in conflict.text
    assert not get(client, "settings")["ai"]["key_configured"]
    configure_ai(client)
    cleared = client.delete(PREFIX + "settings/ai-key")
    assert cleared.is_success and not cleared.json()["ai"]["key_configured"]
    queued = queue.submit(engine, queue.Submission("self_test", "retention", "待执行任务"))
    expired_at = queue.now_ms() - 2 * media.DAY_MS
    with write_transaction(engine) as connection:
        connection.execute(media_cache.update().values(audio_at=expired_at))
        connection.execute(events.update().values(created_at=expired_at))
    media.prune(engine, config, settings["retention"])
    assert not audio.exists()
    assert [row["job_id"] for row in get(client, "events")["items"]] == [queued]
    assert get(client, "topics/" + item["topic_id"])["transcription"]["segments"]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(topics)) == 1
