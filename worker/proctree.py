"""Process-tree helpers (plan §5.4 termination, §5.5 recovery; DECISIONS D-006).

A job is a tree: on Windows the venv python.exe is a launcher whose child is the real
interpreter. "Gone" means gone for the same pid *and* create_time (pids are reused).
"""
from __future__ import annotations

import time
from pathlib import Path

import psutil

VERIFY_TIMEOUT_S = 5.0


def alive(p: psutil.Process) -> bool:
    """True if this exact process (pid + create_time) still runs. psutil compares the
    create_time captured when the Process object was made."""
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def tree(root: psutil.Process) -> list[psutil.Process]:
    """root plus its descendants; [] if root is already gone."""
    try:
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return [root] if alive(root) else []


def kill(procs: list[psutil.Process]) -> None:
    """Kill children before parents (a dying launcher can't respawn anything, but this
    keeps the order predictable). Windows has no SIGTERM: kill() is TerminateProcess."""
    for p in reversed(procs):
        try:
            p.kill()
        except psutil.Error:
            pass


def wait_gone(procs: list[psutil.Process], timeout: float = VERIFY_TIMEOUT_S) -> list[psutil.Process]:
    """Poll until every process is gone or timeout. Returns the survivors."""
    end = time.monotonic() + timeout
    left = [p for p in procs if alive(p)]
    while left and time.monotonic() < end:
        time.sleep(0.05)
        left = [p for p in left if alive(p)]
    return left


def kill_verified(procs: list[psutil.Process], timeout: float = VERIFY_TIMEOUT_S) -> bool:
    """Kill and confirm. Blocking: only for startup recovery, never in the supervisor tick."""
    kill(procs)
    return not wait_gone(procs, timeout)


def find_job_processes(jobs_dir: Path) -> dict[str, list[psutil.Process]]:
    """Every live workload process whose `--job-dir` lies in `jobs_dir`, by job id.

    Scoped to this agent's own jobs directory, so a second agent on the same host
    (start.ps1 -Sim) never touches the other's jobs."""
    root = jobs_dir.resolve()
    found: dict[str, list[psutil.Process]] = {}
    for p in psutil.process_iter(["cmdline"]):
        cmd = p.info.get("cmdline") or []
        if "worker.workloads" not in cmd or "--job-dir" not in cmd or "--job-id" not in cmd:
            continue
        try:
            job_dir = Path(cmd[cmd.index("--job-dir") + 1])
            job_id = cmd[cmd.index("--job-id") + 1]
        except IndexError:
            continue
        if job_dir.resolve().parent == root:
            found.setdefault(job_id, []).append(p)
    return found
