"""Supervisor (plan §5.4): slots pinned to cores, input download, spawn, wait, classify,
timers, RSS watchdog and verified termination.

A slot is held from the moment a job leaves ACCEPTED: first by its input download
(on its own thread, so a slow transfer never blocks the tick), then by its process,
and, if a termination could not be verified, by the `stuck` processes until they are gone.

Termination (cancel, timeout, max_runtime, memory_limit) is a non-blocking state machine
advanced by tick(): cancel sentinel -> 2 s grace -> kill the tree -> poll up to 5 s for
the same pid + create_time to be gone -> record termination_verified and kill_latency_ms.

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
from worker import proctree
from worker.journal import Journal

log = logging.getLogger("dwr.supervisor")

REPO_ROOT = Path(__file__).resolve().parents[1]
ERROR_TAIL_CHARS = 2000
TERM_GRACE_S = 2.0          # cancel sentinel first; the workload exits with code 3 if it can
VERIFY_TIMEOUT_S = 5.0      # after the kill
WATCHDOG_INTERVAL_S = 0.25
RSS_LIMIT_FACTOR = 1.10     # kill when the tree's RSS exceeds 110% of the declared memory

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
    spec: dict
    started: float                                   # time.monotonic() at spawn (plan §5.4 timers)
    files: list[IO] = field(default_factory=list)
    next_rss_check: float = 0.0
    peak_rss_mb: float = 0.0
    # live stats for the playground (D-028): latest tree CPU% (100 = one core) and RSS
    cpu_pct: float | None = None
    rss_mb: float | None = None
    stat_procs: dict = field(default_factory=dict)   # pid -> psutil.Process kept for cpu_percent()
    # termination in progress
    kill_reason: str | None = None
    kill_t0: float | None = None                     # perf_counter when termination began
    kill_sent: float | None = None                   # perf_counter when the tree was killed
    tree: list[psutil.Process] = field(default_factory=list)
    note: str | None = None


@dataclass
class _Stuck:
    """A slot whose job could not be verified gone: not reused until the processes vanish."""
    job_id: str
    slot: int
    procs: list[psutil.Process]


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
        self.jobs_dir = jobs_dir.resolve()  # absolute: children run with cwd=REPO_ROOT
        self.python = python
        self.on_terminal = on_terminal or (lambda: None)
        self.fetch_input = fetch_input or _no_fetch
        self._slots: dict[int, _Running | _Download | _Stuck] = {}
        self._lock = threading.Lock()  # tick() vs request_cancel() from the heartbeat thread

    # ------------------------------------------------------------ queries
    @property
    def slots_total(self) -> int:
        return len(self.slot_cpus)

    def free_slots(self) -> int:
        return self.slots_total - len(self._slots)

    def stuck_slots(self) -> list[int]:
        return sorted(s for s, v in self._slots.items() if isinstance(v, _Stuck))

    def job_stats(self) -> dict[str, dict]:
        """Latest live CPU% (100 = one core, summed over the job's tree) and RSS per running job,
        sampled by the watchdog every 250 ms (playground, D-028)."""
        return {r.job_id: {"cpu_pct": r.cpu_pct, "rss_mb": r.rss_mb}
                for r in list(self._slots.values()) if isinstance(r, _Running) and r.rss_mb is not None}

    def job_processes(self) -> list[psutil.Process]:
        """Root process of every running job (for own-CPU telemetry)."""
        return [r.proc for r in list(self._slots.values()) if isinstance(r, _Running) and r.proc is not None]

    # ------------------------------------------------------------ control
    def request_cancel(self, job_id: str) -> None:
        """Cancel an ACCEPTED job (never spawned -> CANCELLED) or start terminating a RUNNING
        one (-> KILLED(cancel) once verified). Idempotent: replies repeat cancels every beat."""
        with self._lock:
            row = self.journal.get(job_id)
            if row is not None and row["state"] == JobState.RUNNING.value:
                for r in self._slots.values():
                    if isinstance(r, _Running) and r.job_id == job_id:
                        self._begin_termination(r, "cancel")
                return
            if row is None or row["state"] != JobState.ACCEPTED.value:
                return
            for s in self._slots.values():
                if isinstance(s, _Download) and s.job_id == job_id:
                    s.cancel.set()  # the slot is freed when the download thread returns
            self.journal.mark_terminal(job_id, JobState.CANCELLED)
        self.on_terminal()

    def tick(self) -> None:
        with self._lock:
            changed = self._reap()
            self._release_stuck()
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
        self._slots[slot] = _Running(job_id, slot, popen, proc, job_dir, spec, time.monotonic(), files)
        log.info("spawned %s (%s) pid=%s slot=%s cpus=%s", job_id, spec["type"], popen.pid, slot, cpus)
        return False

    def _reap(self) -> bool:
        """Advance every running job: normal exit, limits, termination. True if any became terminal."""
        changed = False
        for slot, r in list(self._slots.items()):
            if not isinstance(r, _Running):
                continue
            rc = r.popen.poll()
            if r.kill_reason is None:
                if rc is not None:
                    self._close(r)
                    del self._slots[slot]
                    state, fields = classify_exit(rc, r.job_dir)
                    self.journal.mark_terminal(r.job_id, state, exit_code=rc, **fields)
                    log.info("job %s exited rc=%s -> %s", r.job_id, rc, state.value)
                    changed = True
                    continue
                self._check_limits(r)
            if r.kill_reason is not None:
                changed |= self._advance_termination(r, rc)
        return changed

    def _check_limits(self, r: _Running) -> None:
        """Plan §5.4: timeout_s and max_runtime_s from spawn (monotonic), RSS every 250 ms."""
        elapsed = time.monotonic() - r.started
        timeout_s = r.spec.get("timeout_s")
        if timeout_s is not None and elapsed > timeout_s:
            self._begin_termination(r, "timeout", f"ran {elapsed:.2f} s > timeout_s {timeout_s}")
            return
        if elapsed > r.spec["max_runtime_s"]:
            self._begin_termination(r, "max_runtime", f"ran {elapsed:.2f} s > max_runtime_s {r.spec['max_runtime_s']}")
            return
        now = time.monotonic()
        if r.proc is None or now < r.next_rss_check:
            return
        r.next_rss_check = now + WATCHDOG_INTERVAL_S
        rss, cpu = 0, 0.0
        live = {}
        for p in proctree.tree(r.proc):
            # cpu_percent() needs the same Process object between calls (first call returns 0).
            q = r.stat_procs.get(p.pid, p)
            live[p.pid] = q
            try:
                rss += q.memory_info().rss
                cpu += q.cpu_percent(None)
            except psutil.Error:
                pass
        r.stat_procs = live
        rss_mb = rss / (1024 * 1024)
        r.peak_rss_mb = max(r.peak_rss_mb, rss_mb)
        r.rss_mb, r.cpu_pct = round(rss_mb, 1), round(cpu, 1)
        limit = RSS_LIMIT_FACTOR * r.spec["mem_mb"]
        if rss_mb > limit:
            self._begin_termination(r, "memory_limit", f"tree RSS {rss_mb:.0f} MB > {limit:.0f} MB "
                                                       f"(110% of declared {r.spec['mem_mb']} MB)")

    def _begin_termination(self, r: _Running, reason: str, note: str | None = None) -> None:
        if r.kill_reason is not None:
            return  # already terminating (a repeated cancel, or a second limit)
        r.kill_reason, r.note, r.kill_t0 = reason, note, time.perf_counter()
        r.tree = proctree.tree(r.proc) if r.proc is not None else []
        try:
            (r.job_dir / "cancel").touch()
        except OSError as e:
            log.warning("could not write cancel sentinel for %s: %s", r.job_id, e)
        log.info("terminating %s: %s%s", r.job_id, reason, f" ({note})" if note else "")

    def _advance_termination(self, r: _Running, rc: int | None) -> bool:
        now = time.perf_counter()
        if r.kill_sent is None:
            if rc is not None and not any(proctree.alive(p) for p in r.tree):
                return self._finish_termination(r, rc, verified=True)  # exited during the grace
            if now - r.kill_t0 < TERM_GRACE_S:
                return False
            if r.proc is not None:  # pick up children spawned since the sentinel
                r.tree = list({p.pid: p for p in [*r.tree, *proctree.tree(r.proc)]}.values())
            proctree.kill(r.tree)
            r.kill_sent = now
            return False
        rc = r.popen.poll()
        if rc is not None and not any(proctree.alive(p) for p in r.tree):
            return self._finish_termination(r, rc, verified=True)
        if now - r.kill_sent > VERIFY_TIMEOUT_S:
            return self._finish_termination(r, rc, verified=False)
        return False

    def _finish_termination(self, r: _Running, rc: int | None, *, verified: bool) -> bool:
        latency_ms = (time.perf_counter() - r.kill_t0) * 1000
        self._close(r)
        result = read_result(r.job_dir)
        if rc == 0 and result is not None:
            # It finished on its own just as the termination began: report the real outcome.
            self.journal.mark_terminal(r.job_id, JobState.SUCCEEDED, exit_code=rc, result=result)
            log.info("job %s finished before its termination (%s) took effect", r.job_id, r.kill_reason)
        else:
            error = r.note or (f"{r.kill_reason} requested")
            if not verified:
                error += "; termination NOT verified, slot marked stuck"
            self.journal.mark_terminal(r.job_id, JobState.KILLED, kill_reason=r.kill_reason, exit_code=rc,
                                       termination_verified=int(verified), kill_latency_ms=latency_ms,
                                       error=error, result=result)
            log.info("job %s KILLED(%s) verified=%s latency=%.0f ms", r.job_id, r.kill_reason, verified, latency_ms)
        if verified:
            del self._slots[r.slot]
        else:
            survivors = [p for p in r.tree if proctree.alive(p)]
            log.error("slot %s is stuck: job %s processes %s did not exit", r.slot, r.job_id,
                      [p.pid for p in survivors])
            self._slots[r.slot] = _Stuck(r.job_id, r.slot, survivors)
        return True

    def _release_stuck(self) -> None:
        for slot, s in list(self._slots.items()):
            if isinstance(s, _Stuck) and not any(proctree.alive(p) for p in s.procs):
                log.warning("stuck slot %s released: job %s processes are gone", slot, s.job_id)
                del self._slots[slot]

    @staticmethod
    def _close(r: _Running) -> None:
        for f in r.files:
            f.close()


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
    """Plan §5.4 exit classification for an exit the supervisor did not cause (its own
    kills are classified in _finish_termination)."""
    result = read_result(job_dir)
    if rc == 0 and result is not None:
        return JobState.SUCCEEDED, {"result": result}
    error = _stderr_tail(job_dir) or (f"exit code {rc}" if rc else "exit 0 without a valid result.json")
    return JobState.FAILED, {"fail_reason": "exit_nonzero", "error": error, "result": result}
