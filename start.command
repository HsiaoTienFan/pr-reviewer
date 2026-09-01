#!/bin/sh
# PR Reviewer launcher — double-click in Finder or run from a terminal.
# Starts the server if it isn't running, then opens the app in your browser.
set -eu
cd "$(dirname "$0")"

PORT="${PORT:-8712}"
URL="http://127.0.0.1:$PORT"
DATA_DIR="$HOME/.pr-reviewer"
LOG="$DATA_DIR/server.log"
PIDFILE="$DATA_DIR/server.pid"

mkdir -p "$DATA_DIR"

is_up() {
    curl -s -o /dev/null -m 2 "$URL/" 2>/dev/null
}

if is_up; then
    echo "PR Reviewer already running at $URL"
    open "$URL"
    exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is not installed (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

echo "Starting PR Reviewer on port $PORT ..."
nohup uv run uvicorn pr_reviewer.app:app --host 127.0.0.1 --port "$PORT" \
    >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"

# wait for the server to come up (up to ~15s)
i=0
while [ "$i" -lt 30 ]; do
    if is_up; then
        echo "Running at $URL  (logs: $LOG)"
        open "$URL"
        exit 0
    fi
    i=$((i + 1))
    sleep 0.5
done

echo "error: server did not start — last log lines:" >&2
tail -n 20 "$LOG" >&2 || true
exit 1
