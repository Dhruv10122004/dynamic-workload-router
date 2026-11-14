"""Host telemetry (plan §5.2): host CPU, own (DWR job) CPU, foreign CPU, memory.

All CPU figures are normalised to the whole host (0-100).
"""
from __future__ import annotations

import psutil


def foreign_cpu_pct(host_cpu_pct: float, own_cpu_pct: float) -> float:
    return max(0.0, host_cpu_pct - own_cpu_pct)


class TelemetrySampler:
    def __init__(self) -> None:
        self._ncpu = psutil.cpu_count() or 1
        self._procs: dict[tuple[int, float], psutil.Process] = {}
        psutil.cpu_percent(interval=None)  # prime: the first reading is meaningless

    def own_cpu_pct(self, roots: list[psutil.Process]) -> float:
        """Sum of CPU% over every process in each job's tree, normalised to the host.

        psutil's per-process cpu_percent() is a delta since the previous call on the same
        Process object, so objects are cached by (pid, create_time)."""
        seen: dict[tuple[int, float], psutil.Process] = {}
        total = 0.0
        for root in roots:
            try:
                tree = [root, *root.children(recursive=True)]
            except psutil.Error:
                continue
            for p in tree:
                try:
                    key = (p.pid, p.create_time())
                    cached = self._procs.get(key)
                    if cached is None:
                        cached = p
                        cached.cpu_percent(interval=None)  # prime; counts from next sample
                    else:
                        total += cached.cpu_percent(interval=None)
                    seen[key] = cached
                except psutil.Error:
                    continue
        self._procs = seen
        return min(100.0, total / self._ncpu)

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
