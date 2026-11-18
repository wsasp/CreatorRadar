import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event
from time import monotonic

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from creator_radar import collector, covers, discovery, queue
from creator_radar.api import create_app
from creator_radar.config import Config
from creator_radar.db import make_engine, migrate, write_transaction
from creator_radar.schema import discoveries, jobs, searches, settings, topics, works

PREFIX = "/api/v1/"
ACCOUNT_URL = "https://space.bilibili.com/434377496"
WORK_URL = "https://www.bilibili.com/video/BV1Fixture01"


@pytest.fixture
def workspace(tmp_path):
    config = Config(workspace="acceptance", data_dir=tmp_path, crawler_fixture=True)
    migrate(config)
    engine = make_engine(config)
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        yield config, engine, client
    engine.dispose()


def run_jobs(config, engine):
    while job := queue.claim(engine, "worker"):
        discovery.execute(engine, config, job, Event())


def post(client, route, body):
    response = client.post(PREFIX + route, json=body)
    assert response.is_success, response.text
    return response.json()


def get(client, route):
    response = client.get(PREFIX + route)
    assert response.is_success, response.text
    return response.json()


def job(client, job_id):
    return get(client, f"jobs/{job_id}")


def account(config, engine, client):
    request = post(client, "accounts", {"url": ACCOUNT_URL, "lookback_days": 5, "scan_limit": 3})
    run_jobs(config, engine)
    return job(client, request["job_id"])["result"]["account_id"]


def test_cached_cover_is_served_for_existing_work(workspace):
    config, engine, client = workspace
    request = post(client, "quick-captures", {"url": WORK_URL})
    run_jobs(config, engine)
    result = job(client, request["job_id"])["result"]
    work_id, topic_id = result["work_id"], result["topic_id"]
    endpoint = f"/api/v1/works/{work_id}/cover"
    assert get(client, f"works/{work_id}")["cover_url"] is None
    assert client.get(endpoint).status_code == 404

    native_id = WORK_URL.rsplit("/", 1)[1]
    cover = covers.path_for(config.workspace_dir, "bilibili", native_id)
    cover.parent.mkdir()
    cover.write_bytes(b"cached cover")
    assert get(client, f"works/{work_id}")["cover_url"] == endpoint
    assert get(client, f"topics/{topic_id}")["work"]["cover_url"] == endpoint
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content == cover.read_bytes()


def test_xhs_detail_serves_ordered_images_across_metadata_only_refresh(workspace, monkeypatch):
    config, engine, client = workspace
    platform = "xiaohongshu"
    note_id = "0123456789abcdef01234567"
    note = f"https://www.xiaohongshu.com/explore/{note_id}"
    first, second = [f"https://sns-webpic.xhscdn.com/{name}" for name in ("first", "second")]
    row = collector.fixture({"operation": "detail", "platform": platform, "url": note})
    monkeypatch.setattr(collector, "run", lambda *_: {
        **row, "image_urls": [first + "?sig=test", "https://other.example/rejected", second, first],
    })
    request = post(client, "quick-captures", {"url": note})
    run_jobs(config, engine)
    result = job(client, request["job_id"])["result"]
    work_id, topic_id = result["work_id"], result["topic_id"]
    endpoint = f"/api/v1/works/{work_id}/images/0"
    cover = covers.path_for(config.workspace_dir, platform, note_id, first)
    cover.parent.mkdir()
    cover.write_bytes(b"cached first image")
    with write_transaction(engine) as connection:
        discovery.upsert_work(connection, platform, row, queue.now_ms())

    work = get(client, f"works/{work_id}")
    assert work["cover_url"] == endpoint
    assert work["image_urls"] == [endpoint, second]
    assert get(client, f"topics/{topic_id}")["work"]["image_urls"] == [endpoint, second]
    assert client.get(endpoint).content == cover.read_bytes()
    assert client.get(f"/api/v1/works/{work_id}/images/2").status_code == 404


def test_remove_search_history_preserves_tasks_and_adopted_topics(workspace):
    config, engine, client = workspace
    first = post(client, "searches", {"keyword": "桌面", "platforms": ["bilibili"]})
    run_jobs(config, engine)
    first_results = f"searches/{first['search_id']}/results"
    work = get(client, first_results)["items"][0]
    topic_id = post(client, "topics", {
        "kind": "search", "work_id": work["id"], "source_id": first["search_id"],
    })["topic_id"]
    pending = post(client, "searches", {"keyword": "收纳", "platforms": ["bilibili"]})

    deleted = client.delete(PREFIX + f"searches/{first['search_id']}")
    assert deleted.json() == {"removed": 1}
    assert [row["id"] for row in get(client, "searches")["items"]] == [pending["search_id"]]
    assert client.get(PREFIX + first_results).status_code == 404
    assert get(client, f"topics/{topic_id}")["source"]["label"] == "桌面"
    assert job(client, first["job_id"])["status"] == "succeeded"

    assert client.delete(PREFIX + "searches").json() == {"removed": 1}
    assert client.delete(PREFIX + "searches").json() == {"removed": 0}
    assert get(client, "searches")["items"] == []
    discovery.schedule(engine)
    run_jobs(config, engine)
    assert job(client, pending["job_id"])["status"] == "succeeded"
    assert client.get(PREFIX + f"searches/{pending['search_id']}/results").status_code == 404
    assert get(client, f"topics/{topic_id}")["work"]["id"] == work["id"]


def test_three_entries_share_one_topic_and_feed_requires_scan(workspace):
    config, engine, client = workspace
    query = post(client, "searches", {"keyword": "桌面", "platforms": ["bilibili"]})
    run_jobs(config, engine)
    results_url = f"searches/{query['search_id']}/results"
    work = get(client, results_url)["items"][0]
    assert work["metrics"]["plays"]["count"] == 0
    assert work["metrics"]["comments"]["count"] is None
    assert not get(client, "feed")["items"]
    source = {"work_id": work["id"], "kind": "search", "source_id": query["search_id"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        topic_ids = list(pool.map(lambda _: post(client, "topics", source)["topic_id"], range(8)))
    assert len(set(topic_ids)) == 1
    topic_id = topic_ids[0]
    assert client.patch(PREFIX + f"topics/{topic_id}", json={"archived": True}).is_success

    with ThreadPoolExecutor(max_workers=4) as pool:
        capture_ids = list(
            pool.map(
                lambda _: post(client, "quick-captures", {"url": WORK_URL + "?token=secret"})[
                    "job_id"
                ],
                range(8),
            )
        )
    assert len(set(capture_ids)) == 1
    run_jobs(config, engine)
    capture = job(client, capture_ids[0])
    assert capture["result"]["topic_id"] == topic_id
    assert "secret" not in str(capture)
    assert get(client, f"topics/{topic_id}")["archived"]
    assert not get(client, "feed")["items"]
    assert get(client, "topics?archived=true")["items"][0]["source"]["kind"] == "search"

    account_id = account(config, engine, client)
    duplicate = post(client, "accounts", {"url": ACCOUNT_URL + "/video"})
    run_jobs(config, engine)
    assert job(client, duplicate["job_id"])["result"]["account_id"] == account_id
    scan = post(client, "account-scans", {"account_ids": [account_id]})
    run_jobs(config, engine)
    assert job(client, scan["job_id"])["result"]["added"] == 1
    results = get(client, f"feed?platform=bilibili&account_id={account_id}&adopted=true&q=收纳")
    assert [item["id"] for item in results["items"]] == [work["id"]]
    with write_transaction(engine) as connection:
        connection.execute(works.update().values(published_at=1000))
    narrowed = f"feed?platform=bilibili&account_id={account_id}&adopted=true&q=收纳"
    assert get(client, f"{narrowed}&since=1000&until=1000")["total"] == 1
    assert get(client, f"{narrowed}&since=1001")["total"] == 0
    assert (
        post(
            client,
            "topics",
            {
                "kind": "scan",
                "work_id": work["id"],
                "source_id": account_id,
            },
        )["topic_id"]
        == topic_id
    )
    assert client.delete(PREFIX + f"accounts/{account_id}").is_success
    assert get(client, "accounts")["items"] == []
    assert get(client, f"feed?account_id={account_id}")["total"] == 1
    assert get(client, f"topics/{topic_id}")["snapshot"]["title"] == work["title"]
    restored = post(client, "accounts", {"url": ACCOUNT_URL})
    run_jobs(config, engine)
    assert job(client, restored["job_id"])["result"]["account_id"] == account_id
    assert get(client, f"accounts/{account_id}")["work_count"] == 1

    with write_transaction(engine) as connection:
        connection.execute(searches.update().values(created_at=1))
        connection.execute(works.update().values(published_at=None))
    discovery.schedule(engine)
    assert client.get(PREFIX + results_url).status_code == 404
    assert get(client, f"topics/{topic_id}")["source"]["label"] == "桌面"
    assert get(client, "feed?since=1")["total"] == 0
    assert get(client, "feed")["total"] == 1


def test_overview_matches_recent_feed_and_tracking(workspace, monkeypatch):
    config, engine, client = workspace
    at = queue.now_ms()
    monkeypatch.setattr(queue, "now_ms", lambda: at)
    account_id = account(config, engine, client)
    post(client, "account-scans", {"account_ids": [account_id]})
    run_jobs(config, engine)
    post(client, "quick-captures", {"url": "https://www.bilibili.com/video/BV1RecentOnly"})
    run_jobs(config, engine)
    assert client.patch(PREFIX + "settings", json={"enabled_platforms": []}).is_success
    with write_transaction(engine) as connection:
        connection.execute(works.update().values(published_at=at - discovery.DAY_MS))
        connection.execute(discoveries.update().values(created_at=at - 2 * discovery.DAY_MS))
    summary = get(client, "overview")
    assert summary["works_24h"] == get(client, "feed?recent=24h")["total"] == 1
    assert summary["tracked_accounts"] == 1
    assert summary["platforms"][-1] == {"platform": "bilibili", "accounts": 1, "works": 1}
    assert summary["active_topics"] == 1
    assert summary["transcription"] == {"completed": 0, "total": 1}
    assert summary["recent_jobs"] == get(client, "jobs?limit=5")["items"]

    for published in (at - discovery.DAY_MS - 1, at + 1, None):
        with write_transaction(engine) as connection:
            connection.execute(works.update().values(published_at=published))
        assert get(client, "overview")["works_24h"] == get(client, "feed?recent=24h")["total"] == 0
    assert client.patch(PREFIX + f"accounts/{account_id}", json={"tracking": "paused"}).is_success
    assert get(client, "overview")["tracked_accounts"] == 0
    assert get(client, "feed")["total"] == 1


def test_partial_search_and_targeted_retry(workspace):
    config, engine, client = workspace
    query = post(
        client, "searches", {"keyword": "分平台验收", "platforms": ["bilibili", "xiaohongshu"]}
    )
    run_jobs(config, engine)
    route = f"searches/{query['search_id']}/results"
    result = get(client, route)
    assert result["search"]["status"] == "partially_succeeded"
    assert [row["platform"] for row in result["items"]] == ["bilibili"]
    failed = next(child for child in result["children"] if child["status"] == "failed")
    post(client, f"jobs/{query['job_id']}/retry", {"child_ids": [failed["id"]]})
    run_jobs(config, engine)
    result = get(client, route)
    assert result["search"]["status"] == "succeeded"
    assert [child["attempt_count"] for child in result["children"]] == [1, 2]
    article = result["items"][1]
    assert article["metrics"]["plays"]["availability"] == "not_applicable"
    assert article["reads"]["count"] == 32
    first = get(client, route + "?limit=1")
    second = get(client, route + "?limit=1&cursor=" + first["next_cursor"])
    assert second["items"][0]["id"] == article["id"]


def test_partial_platform_results_keep_their_order_on_retry(workspace, monkeypatch):
    config, engine, client = workspace

    def collect(_config, request, _keep_running):
        first = collector.fixture({**request, "operation": "detail", "url": WORK_URL})
        second = collector.fixture(
            {
                **request,
                "operation": "detail",
                "url": WORK_URL.replace("01", "02"),
            }
        )
        if request["attempt"] == 1:
            return {"items": [first], "partial_error": "rate_limited"}
        return {"items": [second, first]}

    monkeypatch.setattr(collector, "run", collect)
    query = post(client, "searches", {"keyword": "桌面", "platforms": ["bilibili"]})
    run_jobs(config, engine)
    route = f"searches/{query['search_id']}/results"
    initial = get(client, route)
    assert initial["children"][0]["status"] == "partially_succeeded"
    initial_id = initial["items"][0]["id"]
    post(client, f"jobs/{query['job_id']}/retry", {"child_ids": [initial["children"][0]["id"]]})
    run_jobs(config, engine)
    result = get(client, route)
    assert result["search"]["status"] == "succeeded"
    assert len(result["items"]) == 2
    assert result["items"][0]["id"] == initial_id


def test_resolved_short_and_direct_capture_share_in_flight_job(workspace):
    config, engine, client = workspace
    short = {"url": "https://b23.tv/fixture/"}
    first = post(client, "quick-captures", short)
    run_jobs(config, engine)
    assert job(client, first["job_id"])["status"] == "succeeded"
    second = post(client, "quick-captures", {"url": WORK_URL})
    same = post(client, "quick-captures", short)
    assert second["job_id"] != first["job_id"]
    assert second["job_id"] == same["job_id"]


def test_scan_dispatch_restart_and_platform_filters(workspace):
    config, engine, client = workspace
    first = account(config, engine, client)
    second_job = post(client, "accounts", {"url": "https://space.bilibili.com/42"})
    run_jobs(config, engine)
    second = job(client, second_job["job_id"])["result"]["account_id"]
    assert client.patch(PREFIX + f"accounts/{second}", json={"tracking": "paused"}).is_success
    plan = {"enabled": True, "time": "09:00", "limit": 20, "lookback_days": 7}
    assert client.patch(PREFIX + "settings", json={"scan": plan}).is_success
    assert get(client, "scan-plan")["eligible_accounts"] == 1
    assert client.patch(PREFIX + "settings", json={"scan": {
        **plan, "platforms": ["douyin"],
    }}).is_success
    assert get(client, "scan-plan") == {
        "eligible_accounts": 0, "next_run_at": None, "last_job": None,
    }
    discovery.schedule(engine, datetime(2026, 11, 13, 9, 5))
    assert not get(client, "jobs?kind=account_scan")["items"]
    assert client.patch(PREFIX + "settings", json={"scan": {
        **plan, "platforms": ["bilibili"],
    }}).is_success
    assert get(client, "scan-plan")["next_run_at"]
    discovery.schedule(engine, datetime(2026, 11, 13, 8, 59))
    assert not get(client, "jobs?kind=account_scan")["items"]
    discovery.schedule(engine, datetime(2026, 11, 13, 9, 15))
    run_jobs(config, engine)
    discovery.schedule(engine, datetime(2026, 11, 13, 16, 0))
    scans = get(client, "jobs?kind=account_scan")["items"]
    assert [item["context"] for item in scans] == [{"trigger": "scheduled", "account_id": first}]
    assert get(client, "scan-plan")["last_job"]["id"] == scans[0]["id"]
    scan_events = get(client, f"events?job_id={scans[0]['id']}&level=info&platform=bilibili")
    assert any(event["message"] == "定时扫描已排队" for event in scan_events["items"])
    assert client.patch(PREFIX + "settings", json={"enabled_platforms": []}).is_success
    assert client.post(PREFIX + "quick-captures", json={"url": WORK_URL}).status_code == 409
    discovery.schedule(engine, datetime(2026, 11, 14, 16, 0))
    assert len(get(client, "jobs?kind=account_scan")["items"]) == 1


@pytest.mark.parametrize("invalidated", ["cancel", "lease"])
def test_stale_capture_never_commits(workspace, monkeypatch, invalidated):
    config, engine, client = workspace
    item = post(client, "quick-captures", {"url": WORK_URL})
    claim = queue.claim(engine, "worker")

    def invalidate(_config, request, _checkpoint):
        if invalidated == "cancel":
            queue.cancel(engine, item["job_id"])
        else:
            with write_transaction(engine) as connection:
                connection.execute(
                    jobs.update().where(jobs.c.id == item["job_id"]).values(lease_until=1)
                )
            queue.recover(engine)
        return collector.fixture(request)

    monkeypatch.setattr(collector, "run", invalidate)
    discovery.execute(engine, config, claim, Event())
    assert job(client, item["job_id"])["status"] == (
        "cancelled" if invalidated == "cancel" else "interrupted"
    )
    with engine.connect() as connection:
        for table in (works, discoveries, topics):
            assert connection.scalar(select(func.count()).select_from(table)) == 0


def test_measurement_retains_valid_timestamp_and_link_errors(workspace):
    config, engine, client = workspace
    request = {"operation": "detail", "platform": "bilibili", "url": WORK_URL}
    row = collector.fixture(request)
    with write_transaction(engine) as connection:
        old = discovery.upsert_work(connection, "bilibili", row, 1000)
        row["metrics"] = {}
        latest = discovery.upsert_work(connection, "bilibili", row, 2000)
    assert latest["id"] == old["id"]
    assert latest["metrics"]["plays"] == old["metrics"]["plays"]
    assert latest["metrics"]["plays"]["collected_at"] == 1000
    for url in (
        ACCOUNT_URL,
        "http://127.0.0.1:8765",
        "https://www.bilibili.com.example.com/video/BV1A",
    ):
        response = client.post(PREFIX + "quick-captures", json={"url": url})
        assert response.status_code == 422
    signed = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=secret"
    capture = post(client, "quick-captures", {"url": signed})
    run_jobs(config, engine)
    captured = job(client, capture["job_id"])
    work = get(client, "works/" + captured["result"]["work_id"])
    log = get(client, "events?job_id=" + captured["id"])
    assert "secret" not in str((captured, work, log))
    assert work["original_url"] == signed.split("?")[0]


def test_phase2_migration_preserves_jobs_and_settings(tmp_path):
    config = Config(workspace="acceptance", data_dir=tmp_path)
    config.workspace_dir.mkdir()
    engine = make_engine(config)
    alembic = AlembicConfig()
    alembic.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    with write_transaction(engine) as connection:
        alembic.attributes["connection"] = connection
        command.upgrade(alembic, "0001_workspace")
        connection.execute(
            settings.update()
            .where(settings.c.key == "enabled_platforms")
            .values(
                value=["bilibili"],
            )
        )
    job_id = queue.submit(engine, queue.Submission("self_test", "migration", "旧任务"))
    migrate(config)
    with engine.connect() as connection:
        assert queue.require_job(connection, job_id)["title"] == "旧任务"
        assert discovery.read_settings(connection)["enabled_platforms"] == ["bilibili"]
        assert connection.scalar(select(func.count()).select_from(works)) == 0


def test_media_fetch_stores_a_completed_file(workspace):
    config, engine, client = workspace
    capture = post(client, "quick-captures", {"url": WORK_URL})
    run_jobs(config, engine)
    work_id = job(client, capture["job_id"])["result"]["work_id"]
    media_id = queue.submit(
        engine,
        queue.Submission(
            "media_fetch",
            work_id,
            "媒体验收",
            "bilibili",
            {"work_id": work_id},
        ),
    )
    run_jobs(config, engine)
    result = job(client, media_id)["result"]
    assert result["has_audio"] is True
    target = config.workspace_dir / result["media_path"]
    assert target.stat().st_size == result["bytes"]
    assert not list(target.parent.glob("*.download"))


def test_collector_reclaims_process_tree():
    parent_code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
    )
    start = monotonic()
    with collector.process_tree([sys.executable, "-c", parent_code]) as process:
        assert process.wait(timeout=5) == 0
        with pytest.raises(subprocess.TimeoutExpired):
            process.communicate(timeout=0.2)
    assert process.poll() is not None
    assert monotonic() - start < 10
