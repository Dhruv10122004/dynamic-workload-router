"""Host telemetry (plan §5.2): host CPU, own (DWR job) CPU, foreign CPU, memory.

All CPU figures are normalised to the whole host (0-100).
"""
from __future__ import annotations

import time

import psutil


def foreign_cpu_pct(host_cpu_pct: float, own_cpu_pct: float) -> float:
    return max(0.0, host_cpu_pct - own_cpu_pct)


class TelemetrySampler:
    def __init__(self) -> None:
        self._ncpu = psutil.cpu_count() or 1
        self._cpu_s: dict[tuple[int, float], float] = {}   # (pid, create_time) -> CPU seconds at last sample
        self._last = time.time()
        psutil.cpu_percent(interval=None)  # prime: the first reading is meaningless

    def own_cpu_pct(self, roots: list[psutil.Process]) -> float:
        """CPU used by every process of every job tree since the previous sample, normalised to
        the host (DECISIONS D-029).

        Measured from CPU-time deltas, not psutil's per-object cpu_percent(): that returns 0 the
        first time it sees a process, so a job lasting about one heartbeat was mostly invisible
        and its CPU was counted as *foreign*. A process created since the last sample counts in
        full (all its CPU time falls inside the interval)."""
        now = time.time()
        seen: dict[tuple[int, float], float] = {}
        used = 0.0
        for root in roots:
            try:
                tree = [root, *root.children(recursive=True)]
            except psutil.Error:
                continue
            for p in tree:
                try:
                    created = p.create_time()
                    t = p.cpu_times()
                    total = t.user + t.system
                except psutil.Error:
                    continue
                key = (p.pid, created)
                prev = self._cpu_s.get(key)
                if prev is None:
                    prev = 0.0 if created >= self._last - 0.05 else total   # older but unseen: no baseline
                used += max(0.0, total - prev)
                seen[key] = total
        interval = max(1e-3, now - self._last)
        self._cpu_s, self._last = seen, now
        return min(100.0, 100.0 * used / (interval * self._ncpu))

    def sample(self, roots: list[psutil.Process]) -> dict:
        own = self.own_cpu_pct(roots)
        host = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        return {
            "cpu_pct": host,
            "own_cpu_pct": own,
            "foreign_cpu_pct": foreign_cpu_pct(host, own),
            "mem_total_mb": vm.total // (1024 * 1024),
            "mem_available_mb": vm.available // (1024 * 1024),
        }
