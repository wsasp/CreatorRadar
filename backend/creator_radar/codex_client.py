import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from . import collector
from .analysis_client import ProcessingError

OPTIONS = ("--disable", "apps", "--disable", "plugins", "--disable", "hooks")


def executable():
    path = shutil.which("codex")
    if path and os.name == "nt" and Path(path).suffix.lower() in {".cmd", ".bat"}:
        # Run the npm entry point directly so arguments never pass through cmd.exe.
        entry = Path(path).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if (node := shutil.which("node.exe")) and entry.is_file():
            return [node, str(entry)]
        path = shutil.which("codex.exe")
    if not path:
        raise ProcessingError("ai_codex_unavailable")
    return [path]


async def models():
    command = executable()
    process = None

    async def send(method, *, request_id=None, params=None):
        message = {"method": method}
        if request_id is not None:
            message.update(id=request_id, params=params)
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()

    async def request(request_id, method, params):
        await send(method, request_id=request_id, params=params)
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == request_id:
                return message["result"]
        raise ProcessingError("ai_codex_models_unavailable")

    try:
        async with asyncio.timeout(20):
            with TemporaryDirectory(prefix="creator-radar-codex-") as directory:
                process = await asyncio.create_subprocess_exec(
                    *command, "app-server", *OPTIONS, cwd=directory,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    await request(0, "initialize", {"clientInfo": {
                        "name": "creator_radar", "title": "CreatorRadar 2", "version": "0.2.0",
                    }})
                    await send("initialized")
                    cursor, request_id = None, 1
                    catalog = {}
                    while True:
                        params = {"limit": 50, "includeHidden": False, "cursor": cursor}
                        result = await request(request_id, "model/list", params)
                        for model in result["data"]:
                            if "text" in model.get("inputModalities", ["text"]):
                                catalog[model["model"]] = {
                                    "id": model["model"], "name": model["displayName"],
                                    "reasoning_efforts": [
                                        entry["reasoningEffort"]
                                        for entry in model["supportedReasoningEfforts"]
                                    ],
                                }
                        cursor = result.get("nextCursor")
                        if cursor is None:
                            return list(catalog.values())
                        request_id += 1
                finally:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.communicate()
    except (TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ProcessingError("ai_codex_models_unavailable") from None


def generate(model, effort, messages, schema, keep_running):
    command = executable() + [
        "-a", "never", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
        "--ephemeral", "--json", *OPTIONS, "--disable", "shell_tool",
        "-c", 'web_search="disabled"', "-c", "mcp_servers={}", "-c", "notify=[]",
    ]
    if model:
        command += [f"--model={model}"]
    if effort:
        command += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    prompt = "\n\n".join(message["content"] for message in messages).encode()

    try:
        with TemporaryDirectory(prefix="creator-radar-codex-") as directory:
            schema_path = Path(directory) / "schema.json"
            output_path = Path(directory) / "analysis.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command += ["--output-schema", str(schema_path), "-o", str(output_path), "-"]
            status, output = collector.communicate(
                command, prompt, 300, keep_running, cwd=directory, env=os.environ.copy(),
            )
            if status:
                failure = output.lower()
                if b"401 unauthorized" in failure or b"403 forbidden" in failure:
                    raise ProcessingError("ai_codex_auth")
                if b"429 too many requests" in failure or b"usage limit" in failure:
                    raise ProcessingError("ai_rate_limited")
                raise ProcessingError("ai_codex_failed")
            if not output_path.is_file():
                raise ProcessingError("ai_invalid_output")
            return output_path.read_text(encoding="utf-8")
    except collector.CollectionError as exc:
        code = "ai_timeout" if str(exc) == "timeout" else "execution_interrupted"
        raise ProcessingError(code) from None
    except UnicodeError:
        raise ProcessingError("ai_invalid_output") from None
    except OSError:
        raise ProcessingError("ai_codex_failed") from None
