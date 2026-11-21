"""Shared helpers for the experiment harness: controller client, git metadata, idle wait.

The harness talks to the controller only over HTTP (experiments/CLAUDE.md).
"""
from __future__ import annotations

import os
import statistics
import subprocess
import time
from pathlib import Path

import httpx

from shared.envfile import load_env

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "experiments" / "results"
NON_TERMINAL = ("PENDING", "ASSIGNED", "ACCEPTED", "RUNNING", "UNKNOWN")


def log(msg: str) -> None:
    """Timestamped and flushed: sweeps run for hours with output redirected to a file."""
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def client(url: str, admin_token: str | None = None, env_file: str = ".env") -> httpx.Client:
    """httpx client on <url>/api/v1. Admin token: argument, else DWR_ADMIN_TOKEN from .env
    (not needed from 127.0.0.1)."""
    env = load_env(env_file if os.path.exists(env_file) else None)
    token = admin_token or env.get("DWR_ADMIN_TOKEN")
    headers = {"X-Admin-Token": token} if token else {}
    return httpx.Client(base_url=url.rstrip("/") + "/api/v1", headers=headers, timeout=30)


def git_commit() -> str:
    """HEAD commit, with '-dirty' when tracked files differ (plan §12.3: recorded per run)."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def power_state() -> dict:
    """This host's power source: a laptop on battery runs slower, so every run records it."""
    try:
        import psutil
        b = psutil.sensors_battery()
    except (ImportError, AttributeError, OSError):
        b = None
    return {"battery": b is not None, "plugged_in": None if b is None else bool(b.power_plugged),
            "percent": None if b is None else b.percent}


def non_terminal_count(c: httpx.Client) -> int:
    by_state = c.get("/overview").raise_for_status().json()["by_state"]
    return sum(by_state.get(s, 0) for s in NON_TERMINAL)


def workers_snapshot(c: httpx.Client) -> list[dict]:
    keep = ("worker_id", "tier", "hostname", "os", "os_version", "python", "cpu_logical", "mem_total_mb", "slots",
            "slots_total", "mem_budget_mb", "mem_floor_mb", "supported_types", "capabilities", "agent_version",
            "fresh", "draining")
    return [{k: w.get(k) for k in keep} for w in c.get("/workers").raise_for_status().json()]


def wait_idle(c: httpx.Client, *, require: list[str], cpu_below: float = 10.0, quiet_s: float = 30.0,
              timeout_s: float = 900.0, log=log) -> None:
    """Plan §12.3 between runs: no non-terminal job, the required workers fresh, and every
    fresh worker's host CPU below `cpu_below` % continuously for `quiet_s`."""
    t_end = time.monotonic() + timeout_s
    quiet_since: float | None = None
    last_msg = ""
    while time.monotonic() < t_end:
        busy = non_terminal_count(c)
        workers = c.get("/workers").raise_for_status().json()
        fresh = {w["worker_id"]: w for w in workers if w["fresh"]}
        missing = [w for w in require if w not in fresh]
        hot = {w: (fresh[w]["telemetry"] or {}).get("cpu_pct") for w in fresh
               if ((fresh[w]["telemetry"] or {}).get("cpu_pct") or 0) >= cpu_below}
        if busy or missing or hot:
            quiet_since = None
            msg = f"waiting for idle: non-terminal={busy} missing={missing} cpu>={cpu_below}%={hot}"
            if msg != last_msg:
                log(msg)
                last_msg = msg
        else:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= quiet_s:
                return
        time.sleep(1.0)
    raise TimeoutError(f"system not idle within {timeout_s:.0f} s ({last_msg})")


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (same definition everywhere in the analysis)."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def median(values: list[float]) -> float | None:
    v = [x for x in values if x is not None]
    return statistics.median(v) if v else None
