#!/usr/bin/env bash
# Linux worker setup (plan §16.1, M1): venv, dependencies, .env prompts, connectivity check.
# Usage: ./scripts/setup.sh            (run from anywhere; works on paths with spaces)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- Python 3.10-3.12 ------------------------------------------------------------
PY=""
for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null; then
        PY="$cand"; break
    fi
done
if [ -z "$PY" ]; then
    echo "error: need Python 3.10-3.12 (found: $(python3 --version 2>&1 || echo none))" >&2
    exit 1
fi
echo "using $("$PY" --version) at $(command -v "$PY")"

# --- venv + dependencies -----------------------------------------------------------
# A failed earlier run (no python3-venv) leaves a .venv without pip: rebuild it.
if [ -x "$ROOT/.venv/bin/python" ] && ! "$ROOT/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "existing .venv has no pip; recreating it"
    rm -rf "$ROOT/.venv"
fi
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    "$PY" -m venv "$ROOT/.venv" || {
        rm -rf "$ROOT/.venv"
        echo "error: venv creation failed; on Debian/Ubuntu: sudo apt install ${PY}-venv" >&2; exit 1; }
fi
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
"$ROOT/.venv/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"
echo "dependencies installed"

# --- .env --------------------------------------------------------------------------
ask() {  # ask VAR "prompt" default
    local __v; read -r -p "$2 [$3]: " __v; printf -v "$1" '%s' "${__v:-$3}"
}
if [ -f "$ROOT/.env" ]; then
    echo ".env exists; leaving it unchanged (delete it to re-run the prompts)"
else
    NCPU="$(nproc)"
    # Default: one slot per core, leaving core 0 for the OS and the agent.
    DEFAULT_SLOTS="$(seq -s ';' 1 $((NCPU > 1 ? NCPU - 1 : 0)))"
    [ -n "$DEFAULT_SLOTS" ] || DEFAULT_SLOTS=0
    ask WID   "worker id" "fog-1"
    ask CURL  "controller URL (Windows laptop LAN or Tailscale IP)" "http://192.168.1.10:8000"
    ask TOKEN "worker token (must match DWR_WORKER_TOKENS on the controller)" ""
    ask SLOTS "slot cores (';' between slots, ',' within a slot; host has $NCPU cores)" "$DEFAULT_SLOTS"
    ask BUDGET "memory budget MB" "1536"
    ask FLOOR "memory floor MB" "512"
    if [ -z "$TOKEN" ]; then echo "error: a token is required" >&2; exit 1; fi
    umask 077
    cat > "$ROOT/.env" <<EOF
DWR_WORKER_ID=$WID
DWR_TIER=fog
DWR_CONTROLLER_URL=$CURL
DWR_WORKER_TOKEN=$TOKEN
DWR_SLOT_CPUS=$SLOTS
DWR_MEM_BUDGET_MB=$BUDGET
DWR_MEM_FLOOR_MB=$FLOOR
DWR_DATA_DIR=./var/worker
EOF
    echo "wrote $ROOT/.env (mode 600)"
fi

# --- connectivity check ------------------------------------------------------------
URL="$(grep -E '^DWR_CONTROLLER_URL=' "$ROOT/.env" | cut -d= -f2-)"
echo "checking $URL/api/v1/health ..."
if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 "$URL/api/v1/health"; then
    echo; echo "controller reachable."
else
    echo; echo "WARNING: controller not reachable. Check: controller running and bound to that IP,"
    echo "Windows firewall inbound rule for TCP 8000, same LAN or Tailscale up (plan §14)."
fi
echo "start the worker with:  cd \"$ROOT\" && .venv/bin/python -m worker.agent"
