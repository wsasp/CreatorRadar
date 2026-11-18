#!/bin/bash
set -eu

validation_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
if [[ $(uname -s) != Darwin ]]; then
    echo 'This ASR setup script requires macOS.' >&2
    exit 1
fi
case $(uname -m) in
    arm64) requirements=$validation_dir/processing-macos-arm64.lock ;;
    x86_64) requirements=$validation_dir/processing-macos-x86_64.lock ;;
    *) echo 'Unsupported macOS architecture.' >&2; exit 1 ;;
esac

environment=$validation_dir/../../.phase1/asr-env
python=$environment/bin/python
if [[ ! -x $python ]]; then
    uv venv --python 3.12 "$environment"
fi
uv pip sync --python "$python" "$requirements"
"$python" -X utf8 -B "$validation_dir/processing.py" environment
