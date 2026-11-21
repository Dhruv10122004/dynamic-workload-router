"""cpu_hash: chained SHA-256, single-threaded pure Python (plan §4)."""
from __future__ import annotations

import hashlib
import time

from worker.workloads.common import JobContext

SEED = b"dwr-cpu-hash-v1"


def run(ctx: JobContext, params: dict) -> dict:
    iterations = params["iterations"]
    sha = hashlib.sha256
    data = SEED
    t0 = time.perf_counter()
    for i in range(iterations):
        data = sha(data).digest()
        if not i & 1023:
            ctx.check_cancel()
    return {"exec_s": time.perf_counter() - t0, "checksum": data.hex()}
