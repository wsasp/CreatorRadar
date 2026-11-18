#!/bin/bash
set -eu

usage() {
    echo 'Usage: bash start.sh [--workspace production|acceptance] [--port PORT] [--dev-port PORT] [--data-dir DIR] [--dev]'
}

port_number() {
    local value=$1
    if [[ ! $value =~ ^[0-9]{1,5}$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
        echo "Invalid port: $value" >&2
        return 1
    fi
    echo "$(( 10#$value ))"
}

workspace=production
port=8765
dev_port=5173
data_dir=${CREATOR_RADAR_DATA_DIR:-}
dev=false
while (( $# )); do
    case $1 in
        --workspace|--port|--dev-port|--data-dir)
            if (( $# < 2 )) || [[ $2 == --* ]]; then
                echo "Missing value for $1" >&2
                exit 2
            fi
            case $1 in
                --workspace) workspace=$2 ;;
                --port) port=$(port_number "$2") ;;
                --dev-port) dev_port=$(port_number "$2") ;;
                --data-dir) data_dir=$2 ;;
            esac
            shift 2
            ;;
        --dev) dev=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if [[ $workspace != production && $workspace != acceptance ]]; then
    echo "Invalid workspace: $workspace" >&2
    exit 2
fi
if $dev && [[ $port == "$dev_port" ]]; then
    echo 'The API and Vite ports must differ.' >&2
    exit 2
fi

root=$(cd -- "$(dirname -- "$0")" && pwd -P)
if [[ -n $data_dir ]]; then
    if [[ $data_dir != /* ]]; then data_dir=$PWD/$data_dir; fi
    export CREATOR_RADAR_DATA_DIR=$data_dir
else
    unset CREATOR_RADAR_DATA_DIR
fi
export CREATOR_RADAR_WORKSPACE=$workspace CREATOR_RADAR_PORT=$port
if [[ -n ${CREATOR_RADAR_CRAWLER_RUNTIME:-} && $CREATOR_RADAR_CRAWLER_RUNTIME != /* ]]; then
    export CREATOR_RADAR_CRAWLER_RUNTIME=$PWD/$CREATOR_RADAR_CRAWLER_RUNTIME
fi
if [[ -n ${CREATOR_RADAR_ASR_PYTHON:-} && $CREATOR_RADAR_ASR_PYTHON != /* ]]; then
    export CREATOR_RADAR_ASR_PYTHON=$PWD/$CREATOR_RADAR_ASR_PYTHON
fi
unset CREATOR_RADAR_CRAWLER_SETUP_FAILED
unset CREATOR_RADAR_DEV_PORT
browser_port=$port
if $dev; then
    export CREATOR_RADAR_DEV_PORT=$dev_port
    browser_port=$dev_port
fi

backend=$root/backend
web=$root/web
python=$backend/.venv/bin/python
(cd "$backend" && uv sync --locked && "$python" -X utf8 -m creator_radar migrate)
workspace_path=$(cd "$backend" && "$python" -X utf8 -m creator_radar workspace-path)
logs=$workspace_path/service
mkdir -p -- "$logs"
run_id=$$-$(date +%Y%m%d-%H%M%S)
startup_setup() {
    local name=$1 description=$2 log
    shift 2
    log=$logs/$run_id-$name-setup.log
    echo "$description Setup log: $log"
    if ! (cd "$backend" && "$python" -X utf8 -u -m creator_radar "$@") >"$log" 2>&1; then
        echo "$name setup failed. See $log. Restart to retry." >&2
        return 1
    fi
}
if ! startup_setup crawler 'Checking MediaCrawler (first startup may download Chromium).' setup-crawler --startup; then
    export CREATOR_RADAR_CRAWLER_SETUP_FAILED=true
fi
startup_setup asr 'Preparing faster-whisper small (first startup may download the model).' setup-asr || true
(
    cd "$web"
    npm ci --no-audit --no-fund
    if ! $dev; then npm run build; fi
)

pids=()
cleanup() {
    trap '' INT TERM
    if [[ -n ${pids[0]:-} ]]; then
        kill -TERM "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for role in api worker; do
    (cd "$backend" && exec "$python" -X utf8 -u -m creator_radar "$role") \
        >"$logs/$run_id-$role.out.log" 2>"$logs/$run_id-$role.err.log" &
    pids+=("$!")
done
if $dev; then
    (cd "$web" && exec node node_modules/vite/bin/vite.js) \
        >"$logs/$run_id-vite.out.log" 2>"$logs/$run_id-vite.err.log" &
    pids+=("$!")
fi

echo "Workspace: http://127.0.0.1:$browser_port ($workspace)"
echo "Workspace data: $workspace_path"
echo "Service logs: $logs"
echo 'Press Ctrl+C to stop the local services.'
while true; do
    for pid in "${pids[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            status=0
            wait "$pid" || status=$?
            echo "Service process $pid exited with code $status. See $logs." >&2
            exit 1
        fi
    done
    sleep 0.5
done
