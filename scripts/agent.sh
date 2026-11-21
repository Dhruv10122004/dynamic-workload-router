#!/usr/bin/env bash
# Linux worker agent as a background process (plan §16.1; until the systemd unit is installed).
#   ./scripts/agent.sh start | stop | restart | status
# start: refuses a second copy; detaches fully (safe to call over ssh); logs to var/logs/agent.log.
# stop:  SIGTERM, then SIGKILL after 10 s; then kills any leftover job process of this
#        checkout and verifies nothing is left (AC-7.2). Jobs do not survive (plan §5.5).
# The agent is found by its working directory, so one started by hand is recognised too.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/var/logs/agent.log"
mkdir -p "$ROOT/var/logs"

agent_pid() {  # this checkout's running agent: a worker.agent process whose cwd is ROOT
    local p
    for p in $(pgrep -f -- "-m worker.agent"); do
        [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$ROOT" ] && { echo "$p"; return 0; }
    done
    return 1
}

job_pids() {  # workload processes whose --job-dir lies under this checkout
    pgrep -f -- "worker.workloads .*--job-dir $ROOT/" || true
}

start() {
    local pid
    if pid="$(agent_pid)"; then echo "agent already running (PID $pid)"; return 1; fi
    cd "$ROOT" || return 1
    setsid -f "$PY" -m worker.agent --log-file "$LOG" < /dev/null > /dev/null 2>&1
    for _ in $(seq 1 20); do
        pid="$(agent_pid)" && break
        sleep 0.25
    done
    [ -n "${pid:-}" ] || { echo "agent did not start; see $LOG"; return 1; }
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then echo "agent exited; see $LOG"; tail -5 "$LOG"; return 1; fi
    echo "agent started (PID $pid), log $LOG"
    grep -E "registered|ERROR" "$LOG" | tail -1
}

stop() {
    local pid
    if pid="$(agent_pid)"; then
        kill -TERM "$pid" 2>/dev/null
        for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
        echo "agent stopped (PID $pid)"
    else
        echo "agent not running"
    fi
    local jobs; jobs="$(job_pids)"
    if [ -n "$jobs" ]; then
        echo "killing leftover job processes: $jobs"
        kill -KILL $jobs 2>/dev/null
        sleep 1
    fi
    if [ -n "$(job_pids)" ] || agent_pid >/dev/null; then
        echo "WARNING: processes still alive:"; pgrep -af "worker\.(agent|workloads)"; return 1
    fi
    echo "verified: no agent or job process of this checkout is left"
}

status() {
    local pid
    if pid="$(agent_pid)"; then echo "agent running (PID $pid)"; else echo "agent not running"; fi
    local jobs; jobs="$(job_pids)"
    echo "job processes: ${jobs:-none}"
    tail -3 "$LOG" 2>/dev/null
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    *) echo "usage: $0 start|stop|restart|status"; exit 2 ;;
esac
