#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec uv run --no-project --managed-python --python 3.12 python "$root/packaging/build.py" "$@"
