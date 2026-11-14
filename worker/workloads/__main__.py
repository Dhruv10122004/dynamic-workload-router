"""Entry point: python -m worker.workloads <type> --job-id <id> --job-dir <dir> --cpus <list> --params <json>

Exit codes: 0 success, 2 invalid parameters, 3 cancelled cooperatively, 1 other error.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

from shared.schemas import JobValidationError, validate_job
from worker.workloads import IMPLEMENTED
from worker.workloads.common import (
    EXIT_CANCELLED, EXIT_ERROR, EXIT_INVALID, EXIT_OK, Cancelled, JobContext, peak_rss_mb, pin, write_result,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="worker.workloads")
    ap.add_argument("type")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--job-dir", required=True, type=Path)
    ap.add_argument("--cpus", default="")
    ap.add_argument("--params", default="{}")
    args = ap.parse_args(argv)

    try:
        cpus = [int(c) for c in args.cpus.split(",") if c.strip()]
        if args.type not in IMPLEMENTED:
            raise JobValidationError([{"loc": ("type",), "msg": f"not implemented on this worker: {args.type}"}])
        params = validate_job(args.type, json.loads(args.params), test_mode=True).params
    except (ValueError, JobValidationError) as e:
        print(f"invalid parameters: {e}", file=sys.stderr)
        return EXIT_INVALID

    try:
        pin(cpus)  # before any timed work
        ctx = JobContext(args.job_id, args.job_dir, cpus)
        module = importlib.import_module(IMPLEMENTED[args.type])
        cpu0 = time.process_time()
        result = module.run(ctx, params)
        result["cpu_s"] = time.process_time() - cpu0
        result["peak_rss_mb"] = peak_rss_mb()
        result["pid"] = os.getpid()
        write_result(args.job_dir, result)
        return EXIT_OK
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return EXIT_CANCELLED
    except Exception:
        traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
