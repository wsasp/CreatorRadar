import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from creator_radar import config as configuration
from creator_radar import queue
from creator_radar.api import create_app
from creator_radar.config import Config
from creator_radar.db import make_engine, migrate, write_transaction
from creator_radar.queue import Submission
from creator_radar.schema import events, jobs
from creator_radar.security import redact


@pytest.fixture
def workspace(tmp_path):
    config = Config(workspace="acceptance", data_dir=tmp_path)
    migrate(config)
    migrate(config)
    engine = make_engine(config)
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        yield engine, client
    engine.dispose()


def make_job(key, platform="bilibili"):
    return Submission("self_test", key, "验收", platform)


def test_clear_event_history_preserves_jobs_and_running_batch(workspace):
    engine, client = workspace
    complete = queue.submit(engine, make_job("complete"))
    assert queue.finish(engine, queue.claim(engine, "worker"), "succeeded", "完成")
    running = queue.submit(engine, make_job("batch", None), (
        make_job("first"), make_job("second", "douyin"),
    ))
    assert queue.finish(engine, queue.claim(engine, "worker"), "succeeded", "完成")
    child = queue.claim(engine, "worker")
    assert child["parent_id"] == running
    queued = queue.submit(engine, make_job("queued", "xiaohongshu"))

    def logs(job_id):
        return client.get("/api/v1/events", params={"job_id": job_id}).json()["items"]

    batch_events = logs(running)
    complete_events = logs(complete)
    assert batch_events and complete_events and logs(queued)
    clear = "/api/v1/events/clear"
    assert client.post(clear, json={"scope": "completed"}).json() == {
        "deleted_events": len(complete_events),
    }
    assert logs(complete) == []
    assert logs(queued)
    assert logs(running) == batch_events
    assert client.post(clear, json={"scope": "all"}).json() == {"deleted_events": 1}
    assert logs(queued) == []
    assert logs(running) == batch_events
    assert client.get(f"/api/v1/jobs/{queued}").json()["status"] == "queued"
    detail = client.get(f"/api/v1/jobs/{complete}").json()
    assert detail["status"] == "succeeded" and len(detail["attempts"]) == 1


def test_data_root_keeps_existing_workspaces_and_honors_overrides(tmp_path, monkeypatch):
    user_dir = tmp_path / "user data"
    legacy_dir = tmp_path / ".creator-radar"
    monkeypatch.setattr(configuration, "ROOT", tmp_path)
    monkeypatch.setattr(configuration, "user_data_path", lambda *args, **kwargs: user_dir)
    monkeypatch.delenv("CREATOR_RADAR_DATA_DIR", raising=False)
    assert Config(workspace="production").data_dir == user_dir

    existing = legacy_dir / "acceptance" / "workspace.sqlite3"
    existing.parent.mkdir(parents=True)
    existing.touch()
    assert Config(workspace="acceptance").database_path == existing
    assert Config(workspace="production").data_dir == user_dir

    installed = user_dir / "acceptance" / "workspace.sqlite3"
    installed.parent.mkdir(parents=True)
    installed.touch()
    assert Config(workspace="acceptance").database_path == installed
    monkeypatch.setenv("CREATOR_RADAR_DATA_DIR", str(tmp_path / "显式 目录"))
    assert Config(workspace="acceptance").data_dir == tmp_path / "显式 目录"


def test_schema_and_empty_api(workspace):
    engine, client = workspace
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    for resource in ("feed", "topics", "accounts", "searches", "jobs", "events"):
        expected = {"items": [], "next_cursor": None}
        if resource == "feed":
            expected["total"] = 0
        assert client.get(f"/api/v1/{resource}").json() == expected
    overview = client.get("/api/v1/overview").json()
    assert overview["recent_jobs"] == [] and overview["running_jobs"] == 0
    assert client.get("/api/v1/health").json()["worker"] == "offline"
    assert client.get("/api/v1/settings").json()["scan"]["lookback_days"] == 7


def test_concurrent_dedup_and_platform_claim(workspace):
    engine, _ = workspace
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: queue.submit(engine, make_job("same")), range(20)))
        same_platform = queue.submit(engine, make_job("different"))
        cross_platform = queue.submit(engine, make_job("parallel", "douyin"))
        claims = list(pool.map(lambda index: queue.claim(engine, str(index)), range(12)))
    assert len(set(ids)) == 1
    assert {job["id"] for job in claims if job} == {ids[0], cross_platform}
    assert queue.claim(engine, "next") is None
    claimed = next(job for job in claims if job and job["id"] == ids[0])
    assert queue.finish(engine, claimed, "succeeded", "完成")
    assert queue.claim(engine, "next")["id"] == same_platform


def test_cancel_queue_and_cancel_wins_completion(workspace):
    engine, client = workspace
    pending_id = queue.submit(engine, make_job("queued"))
    running_id = queue.submit(engine, make_job("running"))
    assert client.post(f"/api/v1/jobs/{pending_id}/cancel").status_code == 202
    running = queue.claim(engine, "worker")
    assert running["id"] == running_id
    assert client.post(f"/api/v1/jobs/{running_id}/cancel").status_code == 202
    assert not queue.checkpoint(engine, running)
    assert queue.finish(engine, running, "succeeded", "迟到结果", result={"value": 1})
    for job_id in (pending_id, running_id):
        data = client.get(f"/api/v1/jobs/{job_id}").json()
        assert data["status"] == "cancelled" and data["result"] is None
        assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 409


def test_targeted_batch_retry_preserves_results(workspace):
    engine, client = workspace
    root = queue.submit(engine, Submission("search", "batch", "搜索"), (
        make_job("one", "douyin"), make_job("two", "xiaohongshu"), make_job("three"),
    ))
    outcomes = {"douyin": "succeeded", "xiaohongshu": "failed", "bilibili": "waiting_login"}
    ids = {}
    for _ in range(3):
        claimed = queue.claim(engine, "worker")
        ids[claimed["platform"]] = claimed["id"]
        queue.finish(engine, claimed, outcomes[claimed["platform"]], "完成", result={"value": 1})
    detail = client.get(f"/api/v1/jobs/{root}").json()
    assert detail["status"] == "partially_succeeded"
    assert client.get("/api/v1/jobs?platform=bilibili").json()["items"][0]["id"] == root
    assert client.post(f"/api/v1/jobs/{root}/retry", json={
        "child_ids": [ids["douyin"]],
    }).status_code == 409
    assert client.post(f"/api/v1/jobs/{root}/retry", json={
        "child_ids": [ids["xiaohongshu"]],
    }).status_code == 202
    claimed = queue.claim(engine, "worker")
    assert claimed["id"] == ids["xiaohongshu"] and claimed["attempt_count"] == 2
    queue.finish(engine, claimed, "succeeded", "重试完成")
    detail = client.get(f"/api/v1/jobs/{root}").json()
    children = {child["platform"]: child for child in detail["children"]}
    assert children["douyin"]["result"] == {"value": 1}
    assert children["bilibili"]["status"] == "waiting_login"
    assert children["bilibili"]["attempt_count"] == 1
    history = client.get(f"/api/v1/jobs/{claimed['id']}").json()["attempts"]
    assert [(row["number"], row["status"]) for row in history] == [(2, "succeeded"), (1, "failed")]
    assert len(client.get(f"/api/v1/events?job_id={root}").json()["items"]) > 4


def test_expired_attempt_cannot_write_during_or_after_retry(workspace, monkeypatch):
    engine, client = workspace
    at = queue.now_ms()
    monkeypatch.setattr(queue, "now_ms", lambda: at)
    job_id = queue.submit(engine, make_job("recover"))
    old = queue.claim(engine, "worker")
    at += queue.LEASE_MS
    assert not queue.finish(engine, old, "succeeded", "迟到")
    queue.recover(engine)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "interrupted"
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 202
    new = queue.claim(engine, "worker")
    assert new["attempt_count"] == 2
    assert not queue.checkpoint(engine, old, "迟到")
    assert not queue.finish(engine, old, "succeeded", "迟到")
    assert queue.finish(engine, new, "succeeded", "完成")
    assert client.get(f"/api/v1/jobs/{job_id}").json()["message"] == "完成"


def test_redaction_origins_and_cursor(workspace):
    engine, client = workspace
    markers = ("private-cookie", "second-cookie", "private-bearer", "private-link", "private-key")
    secret = (
        "Cookie: SID=private-cookie; session=second-cookie\n"
        "Authorization: Bearer private-bearer\n"
        "https://example.org/test?xsec_token=private-link\n"
        "'api_key': 'private-key'"
    )
    assert redact(redact(secret)) == redact(secret)
    job_id = queue.submit(engine, Submission("self_test", "log", secret))
    job = queue.claim(engine, "worker")
    queue.finish(engine, job, "failed", secret, result={"api_key": markers[-1]})
    for count in range(3):
        queue.submit(engine, make_job(str(count)))
    with engine.connect() as connection:
        persisted = str(connection.execute(select(events)).all())
        persisted += str(connection.execute(select(jobs)).all())
    history = client.get(f"/api/v1/events?job_id={job_id}").text
    detail = client.get(f"/api/v1/jobs/{job_id}").text
    assert all(marker not in persisted + history + detail for marker in markers)
    assert "已脱敏" in history
    response = client.post(f"/api/v1/jobs/{job_id}/retry", json={"api_key": markers[-1]})
    assert response.status_code == 422 and markers[-1] not in response.text
    invalid = client.get(f"/api/v1/jobs/{markers[-1]}")
    assert invalid.status_code == 422 and markers[-1] not in invalid.text
    assert client.post(f"/api/v1/jobs/{job_id}/retry", headers={
        "origin": "https://other.example",
    }).status_code == 403
    assert client.get("/api/v1/health", headers={"host": "other.example"}).status_code == 400
    assert client.get("/api/v1/health", headers={"host": "localhost:9999"}).status_code == 400
    for resource, cursor_param in (("jobs", "cursor"), ("events", "before")):
        first = client.get(f"/api/v1/{resource}?limit=2").json()
        second = client.get(f"/api/v1/{resource}", params={
            "limit": 2, cursor_param: first["next_cursor"],
        }).json()
        assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}


def eventually(check, timeout=18):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except httpx.TransportError:
            pass
        sleep(0.1)
    raise AssertionError("Timed out waiting for condition")


def spawn(env, role):
    return subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "creator_radar", role],
        cwd=Path(__file__).resolve().parents[1], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False,
            )
        else:
            process.kill()
    process.wait(timeout=10)


def test_real_worker_recovery_and_sse(tmp_path):
    import socket

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    config = Config(workspace="acceptance", data_dir=tmp_path, port=port)
    migrate(config)
    engine = make_engine(config)
    env = {**os.environ, "CREATOR_RADAR_WORKSPACE": "acceptance",
           "CREATOR_RADAR_DATA_DIR": str(tmp_path), "CREATOR_RADAR_PORT": str(port)}
    api = spawn(env, "api")
    processes = [api]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=4) as client:
            eventually(lambda: client.get("/api/v1/health").is_success)
            worker = spawn(env, "worker")
            processes.append(worker)
            eventually(lambda: client.get("/api/v1/health").json()["worker"] == "online")

            def job(key, seconds):
                return queue.submit(engine, Submission(
                    "self_test", key, "验收", "bilibili",
                    {"handler": "acceptance", "seconds": seconds, "scenario": "success"},
                ))

            def status(job_id):
                return client.get(f"/api/v1/jobs/{job_id}").json()["status"]

            interrupted = job("interrupt", 120)
            eventually(lambda: status(interrupted) == "running")
            queued = job("queued", 0.2)
            assert status(queued) == "queued"
            stop(worker)
            replacement = spawn(env, "worker")
            processes.append(replacement)
            eventually(lambda: status(interrupted) == "interrupted")
            eventually(lambda: status(queued) == "succeeded")
            # Shorten the retry workload so the fixture finishes quickly.
            with write_transaction(engine) as connection:
                connection.execute(jobs.update().where(jobs.c.id == interrupted).values(
                    parameters={"handler": "acceptance", "seconds": 0.2, "scenario": "success"},
                ))
            assert client.post(f"/api/v1/jobs/{interrupted}/retry").status_code == 202
            eventually(lambda: status(interrupted) == "succeeded")

            secret = "https://example.org/?token=private-sse"
            bad_id = queue.submit(engine, Submission("self_test", "sse", "日志验收"))
            with write_transaction(engine) as connection:
                row = queue.require_job(connection, bad_id)
                queue.append_event(connection, row, "redaction_test", secret)
            expected = client.get("/api/v1/events?limit=100").json()["items"]
            expected = {item["id"]: item for item in expected}
            cursor = min(expected)
            received = []
            with client.stream("GET", "/api/v1/events/stream?after=0", headers={
                "Last-Event-ID": str(cursor),
            }) as stream:
                assert stream.headers["content-type"].startswith("text/event-stream")
                for line in stream.iter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        received.append(event["id"])
                        if event["code"] == "redaction_test":
                            assert "private-sse" not in line
                            break
            assert received == sorted(i for i in expected if cursor < i <= received[-1])
    finally:
        for process in reversed(processes):
            stop(process)
        engine.dispose()
