"""Run an installed component with its own dependencies and the bundled Python."""

import runpy
import site
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
component, entry, *args = sys.argv[1:]
if component not in {"backend", "crawler", "asr"}:
    raise SystemExit(f"Unknown runtime component: {component}")
site.addsitedir(str(ROOT / "runtime" / "packages" / component))
if component == "backend":
    sys.path.insert(0, str(ROOT / "backend"))
sys.argv = [entry, *args]
if entry == "-m":
    module, *arguments = args
    sys.argv = [module, *arguments]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
else:
    sys.path.insert(0, str(Path(entry).resolve().parent))
    runpy.run_path(entry, run_name="__main__")
