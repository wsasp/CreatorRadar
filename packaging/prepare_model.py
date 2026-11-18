"""Put the pinned default model in the installer's own resource tree."""

import shutil
import sys
from pathlib import Path

from faster_whisper.utils import download_model
from huggingface_hub.errors import LocalEntryNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase1"))
from asr_bridge import WHISPER_REVISION  # noqa: E402

try:
    snapshot = Path(download_model("small", revision=WHISPER_REVISION, local_files_only=True))
    if not (snapshot / "model.bin").is_file():
        raise LocalEntryNotFoundError("Cached small snapshot is incomplete")
except LocalEntryNotFoundError:
    snapshot = Path(download_model("small", revision=WHISPER_REVISION))
target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)
for source in snapshot.iterdir():
    if source.is_file():
        shutil.copy2(source, target / source.name)
for required in ("model.bin", "config.json", "tokenizer.json"):
    if not (target / required).is_file():
        raise SystemExit(f"Missing model resource: {required}")
