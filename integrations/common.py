"""Shared configuration, audio tools, and validation reports for integrations."""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".phase1"
REVISION = "d6f7c5bb906b6dac40ddf343ef9e26438a3de092"
LIMIT = 5


class ProbeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evidence(name, operation, status, **data):
    record = {"operation": operation, "status": status,
              "recorded_at": datetime.now(timezone.utc).isoformat(), **data}
    write_json(STATE / "evidence" / name / f"{operation}.json", record)
    print(f"{name} {operation} {status} {data.get('error', '')}", flush=True)


def run_tool(command, timeout):
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=timeout).stdout
    except FileNotFoundError:
        raise ProbeError("tool_unavailable") from None
    except subprocess.TimeoutExpired:
        raise ProbeError("timeout") from None
    except subprocess.CalledProcessError:
        raise ProbeError("tool_failed") from None


def audio_probe(source):
    raw = run_tool(["ffprobe", "-v", "error", "-show_entries",
                    "format=format_name,duration:stream=codec_type,codec_name,channels,sample_rate",
                    "-of", "json", str(source)], 30)
    info = json.loads(raw)
    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0 or not any(s["codec_type"] == "audio" for s in info.get("streams", [])):
        raise ProbeError("no_audio")
    return info, duration


def extract_audio(source, destination, max_seconds=None):
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0"]
    if max_seconds is not None:
        command.extend(["-t", str(max_seconds)])
    run_tool(command + ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(destination)], 90)
    return audio_probe(destination)[0]
