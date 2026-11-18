#!/bin/bash
set -eu

validation_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
runtime=${1:-${CREATOR_RADAR_CRAWLER_RUNTIME:-}}
if [[ -n $runtime ]]; then
    if [[ $runtime != /* ]]; then runtime=$PWD/$runtime; fi
    set -- --runtime "$runtime"
else
    set --
fi
cd "$validation_dir/../../backend"
exec uv run --locked python -X utf8 -m creator_radar setup-crawler "$@"
