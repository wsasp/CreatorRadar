"""JSON subprocess entry point for the isolated ASR environment."""

import contextlib
import errno
import hashlib
import importlib
import json
import logging
import math
import os
import sys
import wave
from pathlib import Path

from common import ROOT, ProbeError

FUNASR_REVISION = "v2.0.4"
WHISPER_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
WHISPER_CACHE = ROOT / ".phase1" / "asr-models"


def use_project_cache(name):
    return name != "small" and not any(
        os.environ.get(variable) for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME")
    )


def load_model(engine, name, local_only):
    if engine == "faster-whisper":
        from faster_whisper import WhisperModel

        revision = WHISPER_REVISION if name == "small" else None
        kwargs = {"device": "cpu", "compute_type": "int8", "revision": revision,
                  "local_files_only": local_only}
        if name == "small" and os.getenv("CREATOR_RADAR_BUNDLED") == "1":
            kwargs["local_files_only"] = True
            return WhisperModel(str(ROOT / "runtime" / "models" / "small"), **kwargs), revision
        if use_project_cache(name):
            cache = WHISPER_CACHE / "hub"
            if not local_only:
                return WhisperModel(name, download_root=str(cache), **kwargs), revision
            if cache.is_dir():
                try:
                    return WhisperModel(name, download_root=str(cache), **kwargs), revision
                except Exception:
                    pass
        model = WhisperModel(name, **kwargs)
        return model, revision

    from funasr import AutoModel
    from funasr.download.name_maps_from_hub import name_maps_ms
    from modelscope import snapshot_download

    def directory(model, revision):
        if Path(model).is_absolute() and Path(model).is_dir():
            return model
        return snapshot_download(
            name_maps_ms.get(model, model), revision=revision,
            local_files_only=local_only,
        )

    revision = FUNASR_REVISION if name == "paraformer-zh" else None
    model = AutoModel(
        model=directory(name, revision),
        vad_model=directory("fsmn-vad", FUNASR_REVISION),
        punc_model=directory("ct-punc", FUNASR_REVISION),
        vad_kwargs={"max_single_segment_time": 30_000},
        device="cpu", ncpu=4, disable_update=True, trust_remote_code=False,
    )
    return model, revision


def transcribe(model, engine, audio, language):
    import numpy as np

    with wave.open(str(audio), "rb") as pcm:
        if (pcm.getnchannels(), pcm.getsampwidth(), pcm.getframerate()) != (1, 2, 16_000):
            raise ProbeError("audio_invalid")
        duration = pcm.getnframes() / 16_000
        signal = np.frombuffer(pcm.readframes(pcm.getnframes()), dtype="<i2")
    if not signal.size or not signal.any():
        raise ProbeError("no_speech")
    signal = signal.astype(np.float32) / 32768
    if engine == "faster-whisper":
        rows, info = model.transcribe(
            signal, language=None if language == "auto" else language, vad_filter=True,
        )
        language = info.language
        rows = [(row.start, row.end, row.text) for row in rows]
    else:
        result = model.generate(input=signal, fs=16_000, disable_pbar=True,
                                sentence_timestamp=True)[0]
        sentences = result.get("sentence_info") or []
        rows = [(item["start"] / 1000, item["end"] / 1000, item["text"]) for item in sentences]
        if not rows and result.get("text", "").strip():
            stamps = result.get("timestamp") or []
            if not stamps:
                raise ProbeError("asr_invalid_segments")
            rows = [(stamps[0][0] / 1000, stamps[-1][1] / 1000, result["text"])]
    segments = []
    for start, end, text in rows:
        text = text.strip()
        if not text:
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            raise ProbeError("asr_invalid_segments")
        previous_end = segments[-1]["end"] if segments else 0
        start, end = round(max(previous_end, start), 3), round(min(duration, end), 3)
        if start >= end:
            raise ProbeError("asr_invalid_segments")
        segments.append({"start": start, "end": end, "text": text})
    if not segments:
        raise ProbeError("no_speech")
    with audio.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"language": language, "segments": segments, "audio_sha256": digest}


def execute(request):
    engine, name = request["engine"], request["model"]
    check = request["operation"] == "check"
    if engine == "faster-whisper" and use_project_cache(name):
        os.environ.setdefault("HF_XET_CACHE", str(WHISPER_CACHE / "xet"))
    try:
        importlib.import_module({"faster-whisper": "faster_whisper", "FunASR": "funasr"}[engine])
    except Exception:
        if check:
            return {"engine": "unavailable", "model": "unknown"}
        raise ProbeError("asr_engine_unavailable") from None
    try:
        model, revision = load_model(engine, name, local_only=True)
    except Exception:
        if check:
            return {"engine": "ready", "model": "unavailable"}
        try:
            model, revision = load_model(engine, name, local_only=False)
        except Exception as exc:
            if engine == "faster-whisper" and model_disk_full(exc):
                raise ProbeError("asr_model_disk_space") from None
            raise ProbeError("asr_model_unavailable") from None
    if check:
        return {"engine": "ready", "model": "ready"}
    return {
        "engine": engine, "model": name, "model_revision": revision,
        **transcribe(model, engine, Path(request["audio"]), request["language"]),
    }


def model_disk_full(error):
    details = str(error).lower()
    return (getattr(error, "errno", None) == errno.ENOSPC
            or getattr(error, "winerror", None) in {39, 112}
            or any(message in details for message in (
                "no space left on device", "not enough space on the disk", "os error 112", "os error 28",
            )))


def prepare_small():
    try:
        load_model("faster-whisper", "small", local_only=True)
    except Exception:
        print("Downloading faster-whisper small", flush=True)
        load_model("faster-whisper", "small", local_only=False)
    else:
        print("Using cached faster-whisper small", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--prepare-small"]:
        prepare_small()
        sys.exit(0)
    logging.disable(logging.CRITICAL)
    output = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):
            reply = {"data": execute(json.load(sys.stdin))}
    except ProbeError as exc:
        reply = {"error": exc.code}
    except Exception:
        reply = {"error": "asr_failed"}
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False), file=output)
