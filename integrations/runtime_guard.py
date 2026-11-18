import json
import os
from hashlib import file_digest
from pathlib import Path

from common import REVISION, ProbeError, run_tool

MANIFEST = ".creator-radar-manifest.json"


def file_hashes(runtime):
    files = {}
    for path in sorted(runtime.rglob("*")):
        if path.is_symlink():
            raise ProbeError("runtime_modified")
        if path.is_file() and path != runtime / MANIFEST:
            with path.open("rb") as handle:
                digest = file_digest(handle, "sha256").hexdigest()
            files[path.relative_to(runtime).as_posix()] = digest
    return files


def validate_runtime(runtime: Path):
    if os.getenv("CREATOR_RADAR_BUNDLED") == "1":
        try:
            manifest = json.loads((runtime / MANIFEST).read_text(encoding="utf-8"))
            if manifest["revision"] != REVISION:
                raise ProbeError("runtime_revision_mismatch")
            if manifest["files"] != file_hashes(runtime):
                raise ProbeError("runtime_modified")
        except (OSError, ValueError, KeyError, TypeError):
            raise ProbeError("runtime_modified") from None
        return

    if run_tool(["git", "rev-parse", "HEAD"], 10).decode().strip() != REVISION:
        raise ProbeError("runtime_revision_mismatch")
    if run_tool(["git", "status", "--porcelain", "--untracked-files=no"], 10).strip():
        raise ProbeError("runtime_modified")
