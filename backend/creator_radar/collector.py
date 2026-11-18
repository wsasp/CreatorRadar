import ctypes
import json
import os
import shutil
import signal
import subprocess
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from time import monotonic, time

from .config import INTEGRATIONS, VENV_PYTHON, bundle_command
from .links import parse_link

REVISION = "d6f7c5bb906b6dac40ddf343ef9e26438a3de092"
BRIDGE = INTEGRATIONS / "bridge.py"


class CollectionError(Exception):
    pass


def runtime_python(runtime: Path) -> Path:
    return runtime.resolve() / ".venv" / VENV_PYTHON


def fixture_enabled(config):
    return config.workspace == "acceptance" and config.crawler_fixture


def runtime_info(config):
    fixture = fixture_enabled(config)
    if config.bundled:
        installed = (config.crawler_runtime / ".creator-radar-manifest.json").is_file()
    else:
        installed = (runtime_python(config.crawler_runtime).is_file()
                     and (config.crawler_runtime / ".git").exists())
    ready = fixture or (installed and not config.crawler_setup_failed)
    return {
        "state": "fixture" if fixture else "ready" if ready else "unavailable",
        "revision": REVISION,
        "python": "3.12",
    }


@contextmanager
def process_tree(command, *, cwd=None, env=None, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL):
    """Keep child processes owned by the caller, including on Worker exit on Windows."""
    windows = os.name == "nt"
    kernel = handle = None
    if windows:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("per_process_time", ctypes.c_int64),
                ("per_job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("memory", ctypes.c_size_t * 4),
            ]

        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())

    process = None
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=not windows,
            creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
            cwd=cwd,
            env=env if env is not None else {
                key: value
                for key, value in os.environ.items()
                if not key.upper().endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
            },
        )
        if windows and not kernel.AssignProcessToJobObject(handle, int(process._handle)):
            process.kill()
            raise ctypes.WinError(ctypes.get_last_error())
        yield process
    finally:
        if handle:
            kernel.CloseHandle(handle)
        elif process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process:
            process.wait()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


def communicate(command, payload, timeout, keep_running, *, cwd=None, env=None):
    deadline = monotonic() + timeout
    with process_tree(command, cwd=cwd, env=env) as process:
        while keep_running():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CollectionError("timeout")
            try:
                output, _ = process.communicate(payload, timeout=min(1, remaining))
                return process.returncode, output
            except subprocess.TimeoutExpired:
                payload = None
        else:
            raise CollectionError("execution_interrupted")


def json_process(command, request, timeout, keep_running, protocol_error):
    _, output = communicate(
        command, json.dumps(request, ensure_ascii=False).encode(), timeout, keep_running,
    )
    try:
        response = json.loads(output)
        if isinstance(response.get("error"), str):
            raise CollectionError(response["error"])
        if isinstance(response.get("data"), dict):
            return response["data"]
    except (ValueError, AttributeError):
        pass
    raise CollectionError(protocol_error)


def run(config, request, keep_running):
    if fixture_enabled(config):
        if request["operation"] == "media" and config.crawler_fixture_media:
            target = Path(request["target"])
            shutil.copyfile(config.crawler_fixture_media, target)
            return {"bytes": target.stat().st_size, "has_audio": True}
        return fixture(request)
    if runtime_info(config)["state"] == "unavailable":
        raise CollectionError("runtime_unavailable")
    request = {**request, "workspace": str(config.workspace_dir)}
    timeout = {"login": 720, "scan": 145, "search": 145, "media": 180}.get(
        request["operation"], 85,
    )
    python = bundle_command("crawler") if config.bundled else [
        str(runtime_python(config.crawler_runtime)), "-X", "utf8", "-B",
    ]
    command = [*python, str(BRIDGE), str(config.crawler_runtime.resolve())]
    return json_process(command, request, timeout, keep_running, "collector_protocol")


def fixture(request):
    operation, platform = request["operation"], request["platform"]
    if operation == "login":
        return {"state": "ready"}
    if operation == "account":
        link = parse_link(request["url"])
        return {
            "native_id": link.native_id,
            "profile_url": link.public_url,
            "nickname": "验收创作者",
        }
    if (
        operation == "search"
        and request["keyword"] == "分平台验收"
        and (platform == "xiaohongshu" and request["attempt"] == 1)
    ):
        raise CollectionError("rate_limited")
    if operation == "media":
        size = Path(request["target"]).write_bytes(b"acceptance fixture")
        return {"bytes": size, "has_audio": True}
    urls = {
        "bilibili": "https://www.bilibili.com/video/BV1Fixture01",
        "douyin": "https://www.douyin.com/video/123456789",
        "xiaohongshu": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
    }
    link = parse_link(request["url"]) if operation == "detail" else None
    if not link or link.kind == "short":
        link = parse_link(urls[platform])
    article = platform == "xiaohongshu"
    row = {
        "native_id": link.native_id,
        "original_url": link.public_url,
        "collector_url": link.url,
        "kind": "article" if article else "video",
        "title": "桌面创作流程",
        "body": "记录桌面收纳和创作流程。",
        "author": "验收创作者",
        "published_epoch_seconds": int(time()),
        "metrics": {
            key: {
                "count": value,
                "field": key if value is not None else None,
                "availability": "available" if value is not None else "missing",
            }
            for key, value in (
                ("plays", None if article else 0),
                ("likes", 16),
                ("comments", None),
                ("favorites", 2),
                ("shares", 1),
            )
        },
        "reads": {"count": 32, "field": "read_count", "availability": "available"}
        if article
        else None,
    }
    return row if operation == "detail" else {"items": [row]}
