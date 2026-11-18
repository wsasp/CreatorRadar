import asyncio
import json
import math
import re
import shutil
from hashlib import sha256
from threading import Event
from uuid import uuid4

from . import analysis_client as ai
from . import codex_client, collector, funasr_client, media, queue
from . import discovery as d
from .analysis_client import ProcessingError
from .config import ROOT
from .schema import analyses, topics, transcriptions, works
from .transcript_text import simplify_segments

ASR_BRIDGE = ROOT / "phase1" / "asr_bridge.py"
ASR_MAX_SECONDS = 3600
MESSAGES = {
    "video_required": "此作品需要确认为视频后才能转写，请检查原帖并重新采集。",
    "transcript_required": "请先在「逐字稿」完成视频转写。",
    "body_required": "作品缺少可用正文，请重新采集后再生成拆解。",
    "ffmpeg_unavailable": "未找到 FFmpeg，请检查本机安装和 PATH。",
    "ffprobe_unavailable": "未找到 FFprobe，请检查本机安装和 PATH。",
    "audio_invalid": "无法读取有效音轨，请检查媒体或重新采集。",
    "audio_too_long": "视频音轨超过一小时，请选用较短的作品。",
    "audio_timeout": "提取音轨超时，可重试。",
    "media_probe_timeout": "检查媒体音轨超时，可重试。",
    "no_speech": "音轨中未识别到语音。",
    "asr_engine_unavailable": "转写引擎不可用，请在系统设置检查 ASR 环境。",
    "asr_model_unavailable": "转写模型无法加载，请检查模型名称、缓存和网络。",
    "asr_model_disk_space": "模型缓存所在磁盘空间不足，请腾出空间后重试。",
    "asr_timeout": "转写超时，可检查运行环境后重试。",
    "asr_invalid_segments": "转写引擎返回了无效的分段时间或文本。",
    "asr_failed": "转写失败，请检查 ASR 环境后重试。",
    "asr_api_key_required": "请先保存该 FunASR 服务的 API Key。",
    "asr_api_settings_changed": "FunASR 转写设置已更改，请回到选题详情重新提交。",
    "asr_api_network": "无法连接 FunASR 服务，请检查 API 地址和网络。",
    "asr_api_auth": "FunASR 服务拒绝访问，请检查 API Key 和模型权限。",
    "asr_api_audio_too_large": "音轨超过 FunASR 服务的上传大小限制。",
    "asr_api_rate_limited": "FunASR 服务限制了请求，请稍后重试。",
    "asr_api_error": "FunASR 服务返回错误，请检查 API 地址和模型。",
    "asr_api_invalid_output": "FunASR 服务未返回可用的分段时间戳。",
    "ai_configuration_required": "请先在系统设置保存 AI 服务地址和模型。",
    "ai_key_required": "请先在系统设置保存该 AI 服务的 API Key。",
    "credential_unavailable": "无法访问系统凭据存储。",
    "ai_settings_changed": "AI 拆解配置已更改，请回到选题详情重新提交。",
    "ai_codex_unavailable": "未找到 Codex CLI，请安装并将 codex 加入 PATH，再重启工作台。",
    "ai_codex_models_unavailable": "无法读取 Codex CLI 的模型列表，请检查 CLI 版本与登录状态。",
    "ai_codex_auth": "Codex CLI 登录已失效，请运行 codex login 后重试。",
    "ai_codex_failed": "Codex CLI 拆解失败，请检查 CLI 版本、登录状态、模型和思考等级。",
    "ai_input_too_large": "作品文本超过单次拆解上限。",
    "ai_timeout": "AI 调用超时，可重试。",
    "ai_network": "AI 服务连接失败，请检查地址和网络。",
    "ai_auth": "AI 服务拒绝访问，请检查 API Key 和模型权限。",
    "ai_rate_limited": "AI 服务限制了请求，请稍后重试。",
    "ai_http_error": "AI 服务返回错误，请检查服务状态和模型配置。",
    "ai_invalid_output": "AI 返回的拆解格式无效，可重试。",
    "ai_invalid_reference": "AI 返回的依据超出输入范围，可重试。",
    "ai_output_truncated": "AI 返回的拆解不完整，可重试。",
    "execution_interrupted": "任务执行中断，可重试。",
}


def topic_and_work(connection, topic_id):
    topic = d.require_row(connection, topics, topic_id, "找不到该选题。")
    return topic, d.require_row(connection, works, topic["work_id"], "找不到该作品。")


def fingerprint(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode()).hexdigest()


def source_fingerprint(work, transcription_id):
    if work["kind"] == "video" and transcription_id:
        return fingerprint(["video", transcription_id, work["title"], work["body"]])
    if work["kind"] == "article" and (work["body"] or "").strip():
        return fingerprint(["article", work["body"]])
    return None


def transcript_sources(segments):
    return [{"id": index, **segment} for index, segment in enumerate(segments, 1)]


def analysis_snapshot(connection, topic, work):
    if work["kind"] == "video":
        if not topic["transcription_id"]:
            raise ProcessingError("transcript_required")
        row = d.require_row(connection, transcriptions, topic["transcription_id"], "找不到逐字稿。")
        return {
            "kind": "video", "sources": transcript_sources(row["segments"]), "title": work["title"],
            "description": work["body"] or "",
        }
    if work["kind"] != "article" or not (work["body"] or "").strip():
        raise ProcessingError("body_required")
    paragraphs = [line.strip() for line in work["body"].splitlines() if line.strip()]
    return {"kind": "article", "sources": [
        {"id": index, "text": paragraph} for index, paragraph in enumerate(paragraphs, 1)
    ]}


def ai_selection(values):
    if values["provider"] == "codex_cli":
        return {"provider": "codex_cli", "model": values["codex_model"],
                "reasoning_effort": values["codex_reasoning_effort"]}
    return {"provider": "api", "base_url": values["base_url"], "model": values["model"]}


def api_key(config, selection):
    base, model = selection["base_url"], selection["model"]
    if not (base and model):
        raise ProcessingError("ai_configuration_required")
    key = ai.read_key(config, base)
    if not key:
        raise ProcessingError("ai_key_required")
    return key


def funasr_key(config, base_url):
    key = funasr_client.service_key(config, base_url)
    if not key:
        raise ProcessingError("asr_api_key_required")
    return key


async def generate_analysis(config, selection, snapshot, keep_running):
    model = selection["model"]
    if selection["provider"] == "codex_cli":
        cancelled = Event()
        try:
            output = await asyncio.to_thread(
                codex_client.generate, model, selection["reasoning_effort"],
                ai.analysis_messages(snapshot), ai.Analysis.model_json_schema(),
                lambda: not cancelled.is_set() and keep_running(),
            )
        finally:
            cancelled.set()
        return ai.validate_analysis(output, snapshot["sources"])
    return await ai.generate(
        selection["base_url"], model, api_key(config, selection), snapshot, keep_running,
    )


def submit(connection, config, topic_id, kind):
    topic, work = topic_and_work(connection, topic_id)
    job_column = topics.c.asr_job_id if kind == "asr" else topics.c.analysis_job_id
    existing = topic[job_column.name]
    if existing and queue.require_job(connection, existing)["status"] in ("queued", "running"):
        return existing
    values = d.read_settings(connection)
    parameters = {"topic_id": topic_id, "work_id": work["id"], "trigger": "manual"}
    try:
        if kind == "asr":
            if work["kind"] != "video":
                raise ProcessingError("video_required")
            if base_url := values["asr"].get("base_url"):
                funasr_key(config, base_url)
                parameters["asr"] = values["asr"]
        else:
            snapshot = analysis_snapshot(connection, topic, work)
            selection = ai_selection(values["ai"])
            if selection["provider"] == "codex_cli":
                codex_client.executable()
            else:
                api_key(config, selection)
            ai.analysis_messages(snapshot)
            parameters.update(
                **selection, input_snapshot=snapshot,
                transcription_id=topic["transcription_id"] if work["kind"] == "video" else None,
                input_fingerprint=source_fingerprint(work, topic["transcription_id"]),
            )
    except ProcessingError as exc:
        code = str(exc)
        raise d.problem(409, code, MESSAGES[code]) from None
    job_id = queue.submit_in(connection, queue.Submission(
        kind, topic_id, work["title"], work["platform"], parameters,
    ))
    connection.execute(topics.update().where(topics.c.id == topic_id).values({job_column: job_id}))
    return job_id


def topic_results(connection, topic, work, job_view):
    result = {}
    for key, table in (("transcription", transcriptions), ("analysis", analyses)):
        item_id = topic[f"{key}_id"]
        item = None
        if item_id:
            item = dict(d.require_row(connection, table, item_id, "找不到该处理结果。"))
            item["created_at"] = d.stamp(item["created_at"])
        result[key] = item
    if analysis := result["analysis"]:
        analysis["stale"] = analysis["input_fingerprint"] != source_fingerprint(
            work, topic["transcription_id"],
        )
        if not analysis["stale"] and result["transcription"] and work["kind"] == "video":
            analysis["stale"] = analysis["input_snapshot"]["sources"] != transcript_sources(
                result["transcription"]["segments"],
            )
    result["processing_jobs"] = {
        kind: job_view(queue.require_job(connection, topic[f"{kind}_job_id"]))
        if topic[f"{kind}_job_id"] else None for kind in ("asr", "analysis")
    }
    return result


def run_asr(config, request, keep_running):
    python = config.asr_python.absolute()
    if not python.is_file():
        raise ProcessingError("asr_engine_unavailable")
    command = [str(python), "-X", "utf8", "-B", str(ASR_BRIDGE)]
    try:
        return collector.json_process(
            command, request, 60 if request["operation"] == "check" else 1800,
            keep_running, "asr_failed",
        )
    except collector.CollectionError as exc:
        if str(exc) == "timeout":
            raise ProcessingError("asr_timeout") from None
        raise ProcessingError(str(exc)) from None


def check_asr(config, values):
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        state = "unavailable"
        if path:
            try:
                code, _ = collector.communicate([path, "-version"], b"", 5, lambda: True)
                state = "ready" if code == 0 else "unavailable"
            except (collector.CollectionError, OSError):
                pass
        tools[name] = state
    if base_url := values.get("base_url"):
        status = asyncio.run(funasr_client.check(values, funasr_key(config, base_url)))
    else:
        try:
            status = run_asr(config, {**values, "operation": "check"}, lambda: True)
        except (ProcessingError, OSError):
            status = {"engine": "unavailable", "model": "unknown"}
    return {**tools, **status}


def transcribe(engine, config, job, work, values, stage, keep_running):
    platform, work_id = work["platform"], work["id"]
    asr = values["asr"]
    base_url = asr.get("base_url")
    queued_asr = job["parameters"].get("asr")
    if (base_url or queued_asr) and queued_asr != asr:
        raise ProcessingError("asr_api_settings_changed")
    key = funasr_key(config, base_url) if base_url else None
    stage("media", "正在检查缓存和获取视频媒体。")
    ffprobe = shutil.which("ffprobe")
    with engine.connect() as connection:
        video, audio = media.load(connection, config, work_id, values["retention"]["media_days"])

    def duration(path):
        command = [
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path),
        ]
        try:
            code, output = collector.communicate(command, b"", 30, keep_running)
        except collector.CollectionError as exc:
            if str(exc) == "timeout":
                raise ProcessingError("media_probe_timeout") from None
            raise
        if code:
            return None
        try:
            info = json.loads(output)
            seconds = float(info["format"]["duration"])
            if info.get("streams") and math.isfinite(seconds) and seconds > 0:
                return seconds
        except (ValueError, TypeError, KeyError):
            pass
        return None

    def save_file(temporary, target, kind, *, downloaded=False):
        moved = False

        def persist(connection, at):
            nonlocal moved
            temporary.replace(target)
            moved = True
            media.record(connection, config, work_id, kind, target, at)
            if downloaded:
                d.update_session(connection, platform, None, at)

        try:
            if not queue.save_checkpoint(engine, job, persist):
                raise ProcessingError("execution_interrupted")
        except Exception:
            if moved:
                target.unlink(missing_ok=True)
            raise

    if audio is None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not ffprobe:
            raise ProcessingError("ffmpeg_unavailable" if not ffmpeg else "ffprobe_unavailable")
        seconds = duration(video) if video else None
        if seconds is None:
            if platform not in values["enabled_platforms"]:
                raise ProcessingError("platform_disabled")
            target = media.attempt_path(config, job, "media")
            temporary = target.with_suffix(".download")
            try:
                collector.run(config, {
                    "operation": "media", "platform": platform, "url": work["collector_url"],
                    "target": str(temporary), "attempt": job["attempt_count"],
                }, keep_running)
                seconds = duration(temporary)
                if seconds is None:
                    raise ProcessingError("audio_invalid")
                save_file(temporary, target, "media", downloaded=True)
                video = target
            finally:
                temporary.unlink(missing_ok=True)
        if seconds > ASR_MAX_SECONDS:
            raise ProcessingError("audio_too_long")
        stage("audio_extract", "正在提取 16 kHz 单声道音轨。")
        audio = media.attempt_path(config, job, "wav")
        temporary = audio.with_suffix(".partial.wav")
        try:
            command = [
                ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(video),
                "-map", "0:a:0", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(temporary),
            ]
            try:
                code, _ = collector.communicate(command, b"", 180, keep_running)
            except collector.CollectionError as exc:
                if str(exc) == "timeout":
                    raise ProcessingError("audio_timeout") from None
                raise
            if code or not temporary.is_file() or not temporary.stat().st_size:
                def invalidate(connection, _at):
                    media.invalidate(connection, config, work_id, "media", video)

                queue.save_checkpoint(engine, job, invalidate)
                raise ProcessingError("audio_invalid")
            save_file(temporary, audio, "audio")
        finally:
            temporary.unlink(missing_ok=True)
    else:
        stage("audio_extract", "已复用有效音轨。")

    if key is not None:
        stage("transcribing", "正在使用 FunASR API 转写音轨。")
        result = asyncio.run(funasr_client.transcribe(asr, key, audio, keep_running))
    else:
        stage("transcribing", "正在使用本机 ASR 模型转写。")
        result = run_asr(config, {
            **asr, "operation": "transcribe", "audio": str(audio),
        }, keep_running)
    validate_transcript(result)
    result["segments"] = simplify_segments(result["language"], result["segments"])
    return result


def validate_transcript(result):
    try:
        previous = 0
        segments = result["segments"]
        if not segments:
            raise ProcessingError("no_speech")
        if not re.fullmatch(r"[0-9a-f]{64}", result["audio_sha256"]):
            raise ValueError
        for segment in segments:
            start, end = segment["start"], segment["end"]
            if not previous <= start < end <= ASR_MAX_SECONDS or not segment["text"].strip():
                raise ValueError
            previous = end
    except (AttributeError, TypeError, KeyError, ValueError):
        raise ProcessingError("asr_invalid_segments") from None


def execute(engine, config, job, stop):
    parameters = job["parameters"]
    kind, platform = job["kind"], job["platform"]

    def keep_running():
        return not stop.is_set() and queue.checkpoint(engine, job)

    def stage(phase, message):
        if stop.is_set() or not queue.checkpoint(engine, job, phase, message=message):
            raise ProcessingError("execution_interrupted")

    try:
        with engine.connect() as connection:
            _, work = topic_and_work(connection, parameters["topic_id"])
            values = d.read_settings(connection)
        if work["id"] != parameters["work_id"] or work["platform"] != platform:
            raise ProcessingError("execution_error")
        if kind == "asr":
            if work["kind"] != "video":
                raise ProcessingError("video_required")
            result = transcribe(engine, config, job, work, values, stage, keep_running)
        else:
            stage("analyzing", "正在生成并校验结构化拆解。")
            selection = ai_selection(values["ai"])
            queued = {"provider": "api", **parameters}
            if any(queued.get(key) != value for key, value in selection.items()):
                raise ProcessingError("ai_settings_changed")
            snapshot = parameters["input_snapshot"]
            if not snapshot["sources"] or not parameters["input_fingerprint"]:
                raise ProcessingError("body_required")
            result = asyncio.run(generate_analysis(config, selection, snapshot, keep_running))

        def persist(connection, at):
            topic, current_work = topic_and_work(connection, parameters["topic_id"])
            common = {"id": uuid4().hex, "topic_id": topic["id"], "job_id": job["id"],
                      "created_at": at}
            if kind == "asr":
                if current_work["kind"] != "video":
                    raise ProcessingError("video_required")
                connection.execute(transcriptions.insert().values(**common, **result))
                pointer, current = "transcription_id", True
                summary = {"segment_count": len(result["segments"])}
            else:
                connection.execute(analyses.insert().values(
                    **common, model=parameters["model"] or "Codex CLI 默认模型",
                    prompt_version=ai.PROMPT_VERSION,
                    transcription_id=parameters["transcription_id"],
                    input_snapshot=parameters["input_snapshot"], content=result,
                    input_fingerprint=parameters["input_fingerprint"],
                ))
                pointer = "analysis_id"
                current = parameters["input_fingerprint"] == source_fingerprint(
                    current_work, topic["transcription_id"],
                )
                if current and current_work["kind"] == "video":
                    current = analysis_snapshot(connection, topic, current_work)["sources"] == (
                        parameters["input_snapshot"]["sources"]
                    )
                summary = {"current": current}
            if current:
                connection.execute(topics.update().where(
                    topics.c.id == topic["id"],
                ).values({pointer: common["id"]}))
            return {**summary, "version_id": common["id"]}

        if not keep_running():
            raise ProcessingError("execution_interrupted")
        message = "已生成逐字稿。" if kind == "asr" else "已保存拆解结果。"
        queue.finish(engine, job, "succeeded", message, persist=persist)
    except Exception as exc:
        code = str(exc) if isinstance(exc, (ProcessingError, collector.CollectionError)) else ""
        if code not in MESSAGES and code not in d.ERRORS:
            code = "media_unavailable" if code.startswith(("media_", "upstream_")) else (
                "execution_error"
            )
        status = (
            "waiting_login" if code in ("awaiting_login", "challenge", "login_timeout")
            else "interrupted" if code == "execution_interrupted" else "failed"
        )

        def persist_error(connection, at):
            if kind == "asr":
                d.update_session(connection, platform, code, at)

        message = MESSAGES.get(code) or d.ERRORS.get(code)
        if not message:
            message = (
                "无法获取视频媒体，请检查原帖后重试。" if code == "media_unavailable"
                else "处理失败，请查看任务日志后重试。"
            )
        queue.finish(engine, job, status, message, code=code, persist=persist_error)
