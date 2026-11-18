import asyncio
import wave
from contextlib import suppress
from hashlib import file_digest

import httpx

from .analysis_client import ProcessingError, read_key, write_key


def service_key(config, base_url):
    return read_key(config, base_url, namespace="FunASR")


def save_key(config, base_url, key):
    write_key(config, base_url, key, namespace="FunASR")


async def request(base_url, key, keep_running, method, path, *, timeout, **kwargs):
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client,
        ):
            pending = asyncio.create_task(
                client.request(method, base_url + path, headers=headers, **kwargs),
            )
            try:
                while not pending.done():
                    if not keep_running():
                        raise ProcessingError("execution_interrupted")
                    await asyncio.wait({pending}, timeout=1)
                response = pending.result()
            finally:
                if not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError, httpx.HTTPError):
                        await pending
    except (TimeoutError, httpx.TimeoutException):
        raise ProcessingError("asr_timeout") from None
    except httpx.HTTPError:
        raise ProcessingError("asr_api_network") from None
    if not response.is_success:
        code = {
            401: "asr_api_auth", 403: "asr_api_auth", 413: "asr_api_audio_too_large",
            429: "asr_api_rate_limited",
        }.get(response.status_code, "asr_api_error")
        raise ProcessingError(code)
    try:
        return response.json()
    except ValueError:
        raise ProcessingError("asr_api_invalid_output") from None


async def check(values, key):
    result = await request(values["base_url"], key, lambda: True, "GET", "/models", timeout=10)
    try:
        available = any(model["id"] == values["model"] for model in result["data"])
    except (TypeError, KeyError):
        raise ProcessingError("asr_api_invalid_output") from None
    return {"engine": "ready", "model": "ready" if available else "unavailable"}


async def transcribe(values, key, audio, keep_running):
    try:
        with wave.open(str(audio), "rb") as pcm:
            duration = pcm.getnframes() / pcm.getframerate()
        with audio.open("rb") as file:
            digest = file_digest(file, "sha256").hexdigest()
            file.seek(0)
            result = await request(
                values["base_url"], key, keep_running, "POST", "/audio/transcriptions",
                timeout=1800,
                files={"file": ("audio.wav", file, "audio/wav")},
                data={"model": values["model"], "language": values["language"],
                      "response_format": "verbose_json"},
            )
    except (OSError, wave.Error, EOFError):
        raise ProcessingError("audio_invalid") from None

    try:
        rows = result["segments"]
        if rows == [] and not result.get("text", "").strip():
            raise ProcessingError("no_speech")
        segments = []
        for row in rows:
            start, end, text = row["start"], row["end"], row["text"].strip()
            if type(start) not in (int, float) or type(end) not in (int, float):
                raise ValueError
            if key in text or not 0 <= start < end <= duration + 0.1 or start >= duration:
                raise ValueError
            segments.append({"start": start, "end": min(end, duration), "text": text})
        if not segments:
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ProcessingError("asr_api_invalid_output") from None
    return {"engine": "FunASR", "model": values["model"], "model_revision": None,
            "language": values["language"], "segments": segments, "audio_sha256": digest}
