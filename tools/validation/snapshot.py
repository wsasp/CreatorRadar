"""Export the validation records that are designed to be shared without local session data."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "integrations"))
from common import ROOT, STATE, write_json  # noqa: E402


GROUPS = ("runtime", "asr", "ai", "bilibili", "douyin", "xiaohongshu",
          "bilibili-guest", "douyin-guest", "xiaohongshu-guest")


def main():
    groups = {}
    for name in GROUPS:
        records = sorted((STATE / "evidence" / name).glob("*.json"))
        if records:
            groups[name] = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in records}
    write_json(ROOT / "docs" / "phase1-evidence.json", groups)


if __name__ == "__main__":
    main()
