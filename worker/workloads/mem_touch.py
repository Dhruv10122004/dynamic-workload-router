"""mem_touch: allocate mem_mb, touch every page, then hold it for hold_s (plan §4).

exec_s = touch_s + hold_s_actual. The hold sleeps in <= 100 ms steps and checks the
cancel sentinel between them. `actual_mb_override` (test mode only) allocates a different
amount than declared, for the RSS watchdog demo.
"""
from __future__ import annotations

import hashlib
import time

from worker.workloads.common import JobContext

MIB = 1024 * 1024
PAGE = 4096
CHUNK_MB = 16


def run(ctx: JobContext, params: dict) -> dict:
    mb = params.get("actual_mb_override") or params["mem_mb"]
    t0 = time.perf_counter()
    buf = bytearray(mb * MIB)
    pages = 0
    for start in range(0, len(buf), CHUNK_MB * MIB):
        end = min(start + CHUNK_MB * MIB, len(buf))
        n = len(range(start, end, PAGE))
        buf[start:end:PAGE] = b"\x01" * n  # one write per page commits it
        pages += n
        ctx.check_cancel()
    touch_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    ctx.sleep(params["hold_s"])
    hold_s_actual = time.perf_counter() - t1

    touched = sum(buf[::PAGE])  # read back: every page must hold the marker
    return {
        "exec_s": touch_s + hold_s_actual,
        "touch_s": touch_s,
        "hold_s_actual": hold_s_actual,
        "allocated_mb": mb,
        "checksum": hashlib.sha256(f"mem_touch:{mb}:{pages}:{touched}".encode()).hexdigest(),
    }
