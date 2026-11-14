"""Supervisor (plan §5.4): slots pinned to cores, spawn, wait, classify the exit.

M1 scope: spawn/reap/classify and cancel of not-yet-spawned jobs. Timers, the RSS
watchdog and verified termination of running jobs arrive in M4.

A job is a process *tree*: on Windows the venv python.exe is a launcher whose child
is the real interpreter (DECISIONS D-006). The journal records the root PID.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable

import psutil

from shared.states import JobState
from worker.journal import Journal

log = logging.getLogger("dwr.supervisor")

REPO_ROOT = Path(__file__).resolve().parents[1]
ERROR_TAIL_CHARS = 2000


@dataclass
class _Running:
    job_id: str
    slot: int
    popen: subprocess.Popen
    proc: psutil.Process
    job_dir: Path
    files: list[IO] = field(default_factory=list)


class Supervisor:
    def __init__(self, journal: Journal, slot_cpus: list[list[int]], jobs_dir: Path, *,
                 on_terminal: Callable[[], None] | None = None, python: str = sys.executable):
        self.journal = journal
        self.slot_cpus = slot_cpus
        self.jobs_dir = jobs_dir
        self.python = python
        self.on_terminal = on_terminal or (lambda: None)
        self._running: dict[int, _Running] = {}
        self._lock = threading.Lock()  # tick() vs request_cancel() from the heartbeat thread

    # ------------------------------------------------------------ queries
    @property
    def slots_total(self) -> int:
        return len(self.slot_cpus)

    def free_slots(self) -> int:
        return self.slots_total - len(self._running)

    def job_processes(self) -> list[psutil.Process]:
        """Root process of every running job (for own-CPU telemetry)."""
        return [r.proc for r in list(self._running.values()) if r.proc is not None]

    # ------------------------------------------------------------ control
    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            row = self.journal.get(job_id)
            if row is None or row["state"] != JobState.ACCEPTED.value:
                if row is not None and row["state"] == JobState.RUNNING.value:
                    log.warning("cancel of running job %s requested; termination arrives in M4", job_id)
                return
            self.journal.mark_terminal(job_id, JobState.CANCELLED)
        self.on_terminal()

    def tick(self) -> None:
        with self._lock:
            changed = self._reap()
            changed |= self._start_pending()
        if changed:
            self.on_terminal()

    def run(self, stop: threading.Event, interval_s: float = 0.1) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("supervisor tick failed")
            stop.wait(interval_s)

    # ------------------------------------------------------------ internals
    def _start_pending(self) -> bool:
        """Spawn ACCEPTED jobs into free slots. Returns True if a job became terminal."""
        changed = False
        for row in self.journal.accepted_fifo():
            free = [s for s in range(self.slots_total) if s not in self._running]
            if not free:
                break
            changed |= self._spawn(row, free[0])
        return changed

    def _spawn(self, row, slot: int) -> bool:
        job_id = row["job_id"]
        spec = json.loads(row["spec_json"])
        job_dir = self.jobs_dir / job_id
        cpus = self.slot_cpus[slot]
        cmd = [self.python, "-m", "worker.workloads", spec["type"], "--job-id", job_id,
               "--job-dir", str(job_dir), "--cpus", ",".join(map(str, cpus)),
               "--params", json.dumps(spec["params"])]
        files: list[IO] = []
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            files = [open(job_dir / "stdout.log", "wb"), open(job_dir / "stderr.log", "wb")]
            t0 = time.perf_counter()
            popen = subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
                                     stdout=files[0], stderr=files[1])
            spawn_s = time.perf_counter() - t0
        except OSError as e:
            for f in files:
                f.close()
            log.error("spawn of %s failed: %s", job_id, e)
            self.journal.mark_terminal(job_id, JobState.FAILED, fail_reason="spawn_error", error=str(e))
            return True
        try:
            proc = psutil.Process(popen.pid)
            create_time = proc.create_time()
        except psutil.Error:  # exited already; keep a handle-less record
            proc, create_time = None, None
        # Write-ahead step 2: record the PID immediately.
        self.journal.mark_running(job_id, slot=slot, pid=popen.pid, pid_create_time=create_time,
                                  job_dir=str(job_dir), spawn_s=spawn_s)
        self._running[slot] = _Running(job_id, slot, popen, proc, job_dir, files)
        log.info("spawned %s (%s) pid=%s slot=%s cpus=%s", job_id, spec["type"], popen.pid, slot, cpus)
        return False

    def _reap(self) -> bool:
        changed = False
        for slot, r in list(self._running.items()):
            rc = r.popen.poll()
            if rc is None:
                continue
            for f in r.files:
                f.close()
            del self._running[slot]
            state, fields = classify_exit(rc, r.job_dir)
            self.journal.mark_terminal(r.job_id, state, exit_code=rc, **fields)
            log.info("job %s exited rc=%s -> %s", r.job_id, rc, state.value)
            changed = True
        return changed


def read_result(job_dir: Path) -> dict | None:
    try:
        data = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("exec_s"), (int, float)) \
            or "checksum" not in data:
        return None
    return data


def _stderr_tail(job_dir: Path) -> str:
    try:
        return (job_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")[-ERROR_TAIL_CHARS:]
    except OSError:
        return ""


def classify_exit(rc: int, job_dir: Path) -> tuple[JobState, dict]:
    """Plan §5.4 exit classification (M1 subset: no supervisor kills yet)."""
    result = read_result(job_dir)
    if rc == 0 and result is not None:
        return JobState.SUCCEEDED, {"result": result}
    error = _stderr_tail(job_dir) or (f"exit code {rc}" if rc else "exit 0 without a valid result.json")
    return JobState.FAILED, {"fail_reason": "exit_nonzero", "error": error, "result": result}
