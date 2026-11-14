"""Supervisor (plan §5.4): slots pinned to cores, input download, spawn, wait, classify.

Scope so far (M2): download/spawn/reap/classify and cancel of not-yet-spawned jobs.
Timers, the RSS watchdog and verified termination of running jobs arrive in M4.

A slot is held from the moment a job leaves ACCEPTED: first by its input download
(on its own thread, so a slow transfer never blocks the tick), then by its process.

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

# fetch_input(job_id, spec, dest_path, cancel_event) -> (transfer_bytes, transfer_s); raises on failure.
FetchInput = Callable[[str, dict, Path, threading.Event], "tuple[int, float]"]


class TransferCancelled(Exception):
    pass


@dataclass
class _Running:
    job_id: str
    slot: int
    popen: subprocess.Popen
    proc: psutil.Process | None
    job_dir: Path
    files: list[IO] = field(default_factory=list)


@dataclass
class _Download:
    job_id: str
    slot: int
    row: object
    job_dir: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    transfer_bytes: int | None = None
    transfer_s: float | None = None
    error: str | None = None


def _no_fetch(job_id: str, spec: dict, dest: Path, cancel: threading.Event) -> tuple[int, float]:
    raise RuntimeError("this supervisor has no input fetcher")


class Supervisor:
    def __init__(self, journal: Journal, slot_cpus: list[list[int]], jobs_dir: Path, *,
                 on_terminal: Callable[[], None] | None = None, fetch_input: FetchInput | None = None,
                 python: str = sys.executable):
        self.journal = journal
        self.slot_cpus = slot_cpus
        self.jobs_dir = jobs_dir
        self.python = python
        self.on_terminal = on_terminal or (lambda: None)
        self.fetch_input = fetch_input or _no_fetch
        self._slots: dict[int, _Running | _Download] = {}
        self._lock = threading.Lock()  # tick() vs request_cancel() from the heartbeat thread

    # ------------------------------------------------------------ queries
    @property
    def slots_total(self) -> int:
        return len(self.slot_cpus)

    def free_slots(self) -> int:
        return self.slots_total - len(self._slots)

    def job_processes(self) -> list[psutil.Process]:
        """Root process of every running job (for own-CPU telemetry)."""
        return [r.proc for r in list(self._slots.values()) if isinstance(r, _Running) and r.proc is not None]

    # ------------------------------------------------------------ control
    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            row = self.journal.get(job_id)
            if row is None or row["state"] != JobState.ACCEPTED.value:
                if row is not None and row["state"] == JobState.RUNNING.value:
                    log.warning("cancel of running job %s requested; termination arrives in M4", job_id)
                return
            for s in self._slots.values():
                if isinstance(s, _Download) and s.job_id == job_id:
                    s.cancel.set()  # the slot is freed when the download thread returns
            self.journal.mark_terminal(job_id, JobState.CANCELLED)
        self.on_terminal()

    def tick(self) -> None:
        with self._lock:
            changed = self._reap()
            changed |= self._finish_downloads()
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
        """Move ACCEPTED jobs into free slots. Returns True if a job became terminal."""
        changed = False
        busy = {s.job_id for s in self._slots.values()}
        for row in self.journal.accepted_fifo():
            if row["job_id"] in busy:
                continue  # still downloading
            free = [s for s in range(self.slots_total) if s not in self._slots]
            if not free:
                break
            spec = json.loads(row["spec_json"])
            if spec.get("input_bytes"):
                self._start_download(row, free[0])
            else:
                changed |= self._spawn(row, free[0])
        return changed

    def _start_download(self, row, slot: int) -> None:
        job_id = row["job_id"]
        d = _Download(job_id, slot, row, self.jobs_dir / job_id)
        self._slots[slot] = d

        def work() -> None:
            try:
                d.job_dir.mkdir(parents=True, exist_ok=True)
                d.transfer_bytes, d.transfer_s = self.fetch_input(
                    job_id, json.loads(row["spec_json"]), d.job_dir / "input.bin", d.cancel)
            except TransferCancelled:
                d.error = "cancelled"
            except Exception as e:  # any failure is input_transfer_error
                d.error = f"{type(e).__name__}: {e}"
            finally:
                d.done.set()

        threading.Thread(target=work, name=f"download-{job_id[:8]}", daemon=True).start()
        log.info("downloading input for %s (%s bytes) slot=%s", job_id, json.loads(row["spec_json"])["input_bytes"], slot)

    def _finish_downloads(self) -> bool:
        changed = False
        for slot, d in list(self._slots.items()):
            if not isinstance(d, _Download) or not d.done.is_set():
                continue
            del self._slots[slot]
            if self.journal.get(d.job_id)["state"] != JobState.ACCEPTED.value:
                continue  # cancelled while downloading
            if d.error:
                log.error("input download for %s failed: %s", d.job_id, d.error)
                self.journal.mark_terminal(d.job_id, JobState.FAILED, fail_reason="input_transfer_error",
                                           error=d.error, transfer_bytes=d.transfer_bytes, transfer_s=d.transfer_s)
                changed = True
            else:
                changed |= self._spawn(d.row, slot, transfer_bytes=d.transfer_bytes, transfer_s=d.transfer_s)
        return changed

    def _spawn(self, row, slot: int, *, transfer_bytes: int | None = None, transfer_s: float | None = None) -> bool:
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
            self.journal.mark_terminal(job_id, JobState.FAILED, fail_reason="spawn_error", error=str(e),
                                       transfer_bytes=transfer_bytes, transfer_s=transfer_s)
            return True
        try:
            proc = psutil.Process(popen.pid)
            create_time = proc.create_time()
        except psutil.Error:  # exited already; keep a handle-less record
            proc, create_time = None, None
        # Write-ahead step 2: record the PID immediately.
        self.journal.mark_running(job_id, slot=slot, pid=popen.pid, pid_create_time=create_time,
                                  job_dir=str(job_dir), spawn_s=spawn_s, transfer_bytes=transfer_bytes,
                                  transfer_s=transfer_s)
        self._slots[slot] = _Running(job_id, slot, popen, proc, job_dir, files)
        log.info("spawned %s (%s) pid=%s slot=%s cpus=%s", job_id, spec["type"], popen.pid, slot, cpus)
        return False

    def _reap(self) -> bool:
        changed = False
        for slot, r in list(self._slots.items()):
            if not isinstance(r, _Running):
                continue
            rc = r.popen.poll()
            if rc is None:
                continue
            for f in r.files:
                f.close()
            del self._slots[slot]
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
    """Plan §5.4 exit classification (subset until M4: no supervisor kills yet)."""
    result = read_result(job_dir)
    if rc == 0 and result is not None:
        return JobState.SUCCEEDED, {"result": result}
    error = _stderr_tail(job_dir) or (f"exit code {rc}" if rc else "exit 0 without a valid result.json")
    return JobState.FAILED, {"fail_reason": "exit_nonzero", "error": error, "result": result}
