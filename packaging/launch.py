"""Start the installed workstation or verify its offline resources."""

import argparse
import json
import os
import signal
import socket
import subprocess
import webbrowser
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from creator_radar import collector, processing
from creator_radar.config import BUNDLE, Config, bundle_command
from creator_radar.db import migrate
from filelock import FileLock, Timeout


def environment(config):
    tools = [str(BUNDLE / "node"), str(BUNDLE / "media")]
    models = config.data_dir.resolve() / "models"
    return {
        "CREATOR_RADAR_BUNDLED": "1", "CREATOR_RADAR_WORKSPACE": config.workspace,
        "CREATOR_RADAR_DATA_DIR": str(config.data_dir.resolve()),
        "CREATOR_RADAR_PORT": str(config.port),
        "CREATOR_RADAR_CRAWLER_RUNTIME": str(config.crawler_runtime),
        "CREATOR_RADAR_CRAWLER_SETUP_FAILED": "false", "CREATOR_RADAR_CRAWLER_FIXTURE": "false",
        "PLAYWRIGHT_BROWSERS_PATH": str(BUNDLE / "browsers"),
        "HF_HUB_CACHE": os.getenv("HF_HUB_CACHE") or str(models / "hub"),
        "HF_XET_CACHE": os.getenv("HF_XET_CACHE") or str(models / "xet"),
        "PATH": os.pathsep.join([*tools, os.environ.get("PATH", "")]),
    }


def self_test(config, sample):
    os.environ["HF_HUB_OFFLINE"] = "1"
    crawler = collector.run(config, {"platform": "douyin", "operation": "offline"}, lambda: True)
    ffmpeg = BUNDLE / "media" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    with TemporaryDirectory(prefix="creator-radar-asr-") as directory:
        audio = Path(directory) / "sample.wav"
        subprocess.run([
            ffmpeg, "-nostdin", "-v", "error", "-i", sample, "-map", "0:a:0",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio,
        ], check=True, timeout=60)
        result = processing.run_asr(config, {
            "operation": "transcribe", "engine": "faster-whisper", "model": "small",
            "language": "zh", "audio": str(audio),
        }, lambda: True)
    processing.validate_transcript(result)
    print(json.dumps({"crawler": crawler, "model": result["model"],
                      "segments": len(result["segments"])}, ensure_ascii=False))


def terminate(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def ensure_running(processes, logs):
    for role, process in processes.items():
        if (code := process.poll()) is not None:
            raise SystemExit(f"{role} exited with code {code}. Logs: {logs}")


def serve(config, open_browser):
    url = f"http://127.0.0.1:{config.port}"
    with socket.socket() as test_port:
        if os.name == "nt":
            test_port.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        test_port.bind(("127.0.0.1", config.port))
    logs = config.workspace_dir / "service"
    logs.mkdir(parents=True, exist_ok=True)
    migrate(config)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    processes = {}
    opener = build_opener(ProxyHandler({}))
    with ExitStack() as stack:
        for role in ("worker", "api"):
            log = stack.enter_context((logs / f"{stamp}-{role}.log").open("ab"))
            process = stack.enter_context(collector.process_tree(
                [*bundle_command("backend"), "-m", "creator_radar", role],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            ))
            stack.callback(terminate, process)
            processes[role] = process
        deadline = monotonic() + 30
        while monotonic() < deadline:
            ensure_running(processes, logs)
            try:
                with opener.open(url + "/api/v1/health", timeout=1) as response:
                    if json.load(response)["worker"] == "online":
                        break
            except (OSError, URLError, ValueError):
                pass
            sleep(0.25)
        else:
            raise SystemExit(f"Startup timed out. Logs: {logs}")
        print(f"CreatorRadar: {url}\nLogs: {logs}\nPress Ctrl+C to stop.", flush=True)
        if open_browser:
            webbrowser.open(url)
        while True:
            ensure_running(processes, logs)
            sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", choices=("production", "acceptance"), default="production")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", metavar="AUDIO", type=Path)
    args = parser.parse_args()
    options = {"data_dir": args.data_dir} if args.data_dir else {}
    config = Config(bundled=True, crawler_runtime=BUNDLE / "MediaCrawler", workspace=args.workspace,
                    port=args.port, dev_port=None, crawler_fixture=False,
                    crawler_setup_failed=False, **options)
    os.environ.pop("CREATOR_RADAR_DEV_PORT", None)
    os.environ.update(environment(config))
    if args.self_test:
        self_test(config, args.self_test.resolve(strict=True))
        return

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, stop)
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(config.workspace_dir / "workstation.lock", timeout=0):
            serve(config, not args.no_browser)
    except Timeout:
        raise SystemExit("CreatorRadar is already running for this workspace.") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
