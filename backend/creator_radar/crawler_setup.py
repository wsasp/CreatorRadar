import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock

from .collector import REVISION, fixture_enabled, runtime_python
from .config import ROOT, Config

REPOSITORY = "https://github.com/NanmiCoder/MediaCrawler.git"
PROBE = ROOT / "phase1" / "crawler.py"


def check_checkout(runtime: Path) -> None:
    if not (runtime / ".git").exists():
        raise ValueError(f"MediaCrawler path is not a Git checkout: {runtime}")
    revision = subprocess.check_output(["git", "-C", runtime, "rev-parse", "HEAD"]).strip().decode()
    if revision != REVISION:
        raise ValueError(f"MediaCrawler must be at {REVISION}: {runtime}")
    if subprocess.check_output([
        "git", "-C", runtime, "status", "--porcelain", "--untracked-files=no",
    ]).strip():
        raise ValueError(f"MediaCrawler has modified tracked files: {runtime}")


def install_crawler(
    config: Config, runtime_override: Path | None = None, *, startup: bool = False,
) -> None:
    if startup and fixture_enabled(config):
        print("Using the acceptance crawler fixture", flush=True)
        return

    runtime = (runtime_override or config.crawler_runtime).resolve()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(runtime.with_name(runtime.name + ".install.lock"), timeout=600):
        if not runtime.exists():
            print(f"Downloading MediaCrawler into {runtime}", flush=True)
            with TemporaryDirectory(prefix=".mediacrawler-", dir=runtime.parent) as staging:
                checkout = Path(staging) / "checkout"
                subprocess.run([
                    "git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, checkout,
                ], check=True)
                subprocess.run(
                    ["git", "-C", checkout, "checkout", "--detach", REVISION], check=True,
                )
                checkout.rename(runtime)

        check_checkout(runtime)
        python = runtime_python(runtime)
        probe = [python, "-X", "utf8", "-B", PROBE, "--runtime", runtime, "offline"]
        if python.is_file():
            try:
                if subprocess.run(probe, check=False).returncode == 0:
                    print(f"Using existing MediaCrawler at {runtime}", flush=True)
                    return
            except OSError:
                # A stale virtual environment can contain an invalid interpreter launcher.
                pass

        print("Installing MediaCrawler dependencies and Chromium", flush=True)
        subprocess.run(
            ["uv", "sync", "--frozen", "--project", runtime, "--python", "3.12", "--reinstall"],
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(runtime / ".venv")},
            check=True,
        )
        subprocess.run(
            [python, "-m", "playwright", "install", "chromium"],
            env={**os.environ, "PLAYWRIGHT_SKIP_BROWSER_GC": "1"},
            check=True,
        )
        subprocess.run(probe, check=True)
