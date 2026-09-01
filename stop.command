#!/bin/sh
# PR Reviewer stopper — shuts down the server started by start.command.
set -eu

PORT="${PORT:-8712}"
PIDFILE="$HOME/.pr-reviewer/server.pid"

stopped=""

if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE")"
    if kill "$PID" 2>/dev/null; then
        echo "Stopped PR Reviewer (pid $PID)"
        stopped=1
    fi
    rm -f "$PIDFILE"
fi

# fallback: anything still listening on the port
if [ -z "$stopped" ]; then
    PIDS="$(lsof -ti "tcp:$PORT" 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
        # shellcheck disable=SC2086
        kill $PIDS 2>/dev/null || true
        echo "Stopped process(es) on port $PORT: $PIDS"
        stopped=1
    fi
fi

[ -n "$stopped" ] || echo "PR Reviewer is not running."
