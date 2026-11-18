import signal
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import uuid4

from . import discovery, media, processing
from .queue import checkpoint, claim, finish, heartbeat, recover
from .schema import PROCESSING_TYPES

PARALLEL_JOBS = 3
HEARTBEAT_SECONDS = 1


def execute(engine, config, job, stop):
    handler = job["parameters"].get("handler")
    acceptance = handler == "acceptance"
    if not acceptance and job["kind"] in PROCESSING_TYPES:
        processing.execute(engine, config, job, stop)
        return
    if not acceptance and job["kind"] in discovery.OPERATIONS:
        discovery.execute(engine, config, job, stop)
        return
    try:
        if not checkpoint(engine, job, "executing"):
            finish(engine, job, "interrupted", "执行中断，可重试", code="execution_interrupted")
            return
        if job["kind"] == "self_test" and handler == "self_test":
            finish(engine, job, "succeeded", "队列自检通过", result={"verified": True})
            return
        if config.workspace != "acceptance" or not acceptance:
            finish(engine, job, "failed", "任务执行器尚未接入", code="handler_unavailable")
            return
        deadline = monotonic() + job["parameters"]["seconds"]
        while (remaining := deadline - monotonic()) > 0:
            if stop.wait(min(HEARTBEAT_SECONDS, remaining)) or not checkpoint(engine, job):
                finish(engine, job, "interrupted", "Worker 已停止，可重试", code="worker_stopped")
                return
        scenario = job["parameters"]["scenario"]
        if scenario == "fail_once" and job["attempt_count"] == 1:
            finish(engine, job, "failed", "验收任务首次执行失败", code="acceptance_failure")
        elif scenario == "waiting_login" and job["attempt_count"] == 1:
            finish(engine, job, "waiting_login", "等待平台登录后重试", code="login_required")
        else:
            finish(engine, job, "succeeded", "验收任务执行完成", result={"verified": True})
    except Exception:
        # Adapter exceptions can include raw URLs, headers and request bodies.
        finish(engine, job, "failed", "执行任务时发生异常", code="execution_error")


def run(engine, config):
    worker_id = uuid4().hex
    stop = Event()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous_handler = signal.signal(signal.SIGTERM, request_stop)
    active = set()
    next_heartbeat = 0.0
    next_schedule = 0.0
    next_cleanup = 0.0
    with ThreadPoolExecutor(max_workers=PARALLEL_JOBS) as pool:
        try:
            while not stopping:
                if monotonic() >= next_heartbeat:
                    heartbeat(engine, worker_id)
                    recover(engine)
                    next_heartbeat = monotonic() + HEARTBEAT_SECONDS
                if monotonic() >= next_schedule:
                    discovery.schedule(engine)
                    next_schedule = monotonic() + 5
                if monotonic() >= next_cleanup:
                    with engine.connect() as connection:
                        retention = discovery.read_settings(connection)["retention"]
                    media.prune(engine, config, retention)
                    next_cleanup = monotonic() + 60
                for task in {task for task in active if task.done()}:
                    task.result()
                    active.remove(task)
                while (
                    not stopping
                    and len(active) < PARALLEL_JOBS
                    and (job := claim(engine, worker_id))
                ):
                    active.add(pool.submit(execute, engine, config, job, stop))
                stop.wait(0.25)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            signal.signal(signal.SIGTERM, previous_handler)
