"""data_digest: read input.bin into memory and hash it `rounds` times (plan §4).

exec_s covers reading the file and hashing; the download is measured separately by
the supervisor as transfer_s.
"""
from __future__ import annotations

import hashlib
import time

from worker.workloads.common import JobContext

CHUNK = 1024 * 1024


def run(ctx: JobContext, params: dict) -> dict:
    expected = params["input_bytes"]
    t0 = time.perf_counter()
    if expected:
        with open(ctx.job_dir / "input.bin", "rb") as f:
            data = f.read()
    else:
        data = b""
    if len(data) != expected:
        raise ValueError(f"input.bin has {len(data)} bytes, expected {expected}")
    view = memoryview(data)
    digest = b""
    for _ in range(params["rounds"]):
        h = hashlib.sha256(digest)
        for i in range(0, len(view), CHUNK):
            h.update(view[i:i + CHUNK])
            ctx.check_cancel()
        digest = h.digest()
        ctx.check_cancel()
    return {
        "exec_s": time.perf_counter() - t0,
        "checksum": digest.hex(),
        "bytes_hashed": len(data) * params["rounds"],
    }
