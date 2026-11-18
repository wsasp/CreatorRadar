import os
import subprocess

from filelock import FileLock

from .collector import fixture_enabled
from .config import DEFAULT_ASR_PYTHON, INTEGRATIONS, Config
from .processing import ASR_BRIDGE


def prepare_asr(config: Config) -> None:
    if fixture_enabled(config) and not config.crawler_fixture_media:
        print("Skipping ASR setup for the queue-only acceptance fixture", flush=True)
        return

    python = config.asr_python.absolute()
    environment = DEFAULT_ASR_PYTHON.parent.parent
    environment.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(environment.with_name(environment.name + ".install.lock"), timeout=600):
        engine_check = [python, "-X", "utf8", "-c", "from faster_whisper import WhisperModel"]
        installed = False
        if python.is_file():
            try:
                installed = subprocess.run(engine_check, check=False).returncode == 0
            except OSError:
                pass
        if not installed:
            if python != DEFAULT_ASR_PYTHON:
                raise ValueError(f"Install faster-whisper in CREATOR_RADAR_ASR_PYTHON: {python}")
            print("Installing the faster-whisper CPU environment", flush=True)
            if not python.is_file():
                subprocess.run(["uv", "venv", "--python", "3.12", environment], check=True)
            subprocess.run([
                "uv", "pip", "install", "--python", python, "--reinstall",
                "-r", INTEGRATIONS / "faster-whisper.lock",
            ], check=True)
        # The bridge and the Worker share the same Hugging Face cache and model revision.
        subprocess.run(
            [python, "-X", "utf8", "-B", ASR_BRIDGE, "--prepare-small"],
            env={**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"},
            check=True,
        )
