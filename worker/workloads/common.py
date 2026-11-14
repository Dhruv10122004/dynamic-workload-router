"""Workload process contract helpers (plan §4): pinning, cancel sentinel, atomic result."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import psutil

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID = 2
EXIT_CANCELLED = 3

CANCEL_CHECK_INTERVAL_S = 0.05  # contract: at least every 100 ms


class Cancelled(Exception):
    pass


class JobContext:
    def __init__(self, job_id: str, job_dir: Path, cpus: list[int]):
        self.job_id = job_id
        self.job_dir = job_dir
        self.cpus = cpus
        self._cancel_path = job_dir / "cancel"
        self._next_check = 0.0

    def check_cancel(self) -> None:
        """Raise Cancelled if the sentinel exists. Cheap to call often: the file is
        stat-ed at most every CANCEL_CHECK_INTERVAL_S."""
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + CANCEL_CHECK_INTERVAL_S
        if self._cancel_path.exists():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        """Sleep in steps of <= 100 ms, checking the cancel sentinel between steps.

        The deadline uses perf_counter (the clock exec_s is measured with): on Windows
        time.monotonic ticks every ~15.6 ms, which could end the hold early."""
        end = time.perf_counter() + seconds
        while True:
            self._next_check = 0.0
            self.check_cancel()
            left = end - time.perf_counter()
            if left <= 0:
                return
            time.sleep(min(0.1, left))


def pin(cpus: list[int]) -> None:
    if cpus:
        psutil.Process().cpu_affinity(cpus)


def peak_rss_mb() -> float:
    """Linux: ru_maxrss (KiB). Windows: peak working set (labelled as such in the UI)."""
    if sys.platform.startswith("linux"):
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    return psutil.Process().memory_info().peak_wset / (1024 * 1024)


def write_result(job_dir: Path, data: dict) -> None:
    tmp = job_dir / "result.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, job_dir / "result.json")
