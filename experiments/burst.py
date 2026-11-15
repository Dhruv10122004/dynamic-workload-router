"""Submit N jobs at once and report where they went (AC-3.2, AC-3.3 on the real machines).

    python -m experiments.burst --n 100 [--mix cpu|mem|mixed] [--url http://127.0.0.1:8000]

Prints, per worker, how many jobs it ran, how many waited (overflow), the ineligible
reasons seen at first attempts, and the final states. Then run `python -m experiments.audit`
for the no-overbooking check. Admin calls from 127.0.0.1 need no token; elsewhere pass
--admin-token (or DWR_ADMIN_TOKEN in .env).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

from shared.envfile import load_env

TERMINAL = {"SUCCEEDED", "FAILED", "KILLED", "CANCELLED", "REJECTED"}
MIXES = {
    "cpu": [("cpu_hash", {"iterations": 1_000_000})],
    "mem": [("mem_touch", {"mem_mb": 256, "hold_s": 3})],
    "mixed": [("cpu_hash", {"iterations": 1_000_000}), ("mem_touch", {"mem_mb": 256, "hold_s": 3}),
              ("data_digest", {"input_bytes": 8 * 1024 * 1024, "rounds": 2})],
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Burst submission + placement report")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--mix", choices=sorted(MIXES), default="cpu")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin-token")
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--timeout", type=float, default=900, help="seconds to wait for all jobs")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args(argv)
    env = load_env(a.env_file if os.path.exists(a.env_file) else None)
    token = a.admin_token or env.get("DWR_ADMIN_TOKEN")
    headers = {"X-Admin-Token": token} if token else {}
    mix = MIXES[a.mix]

    with httpx.Client(base_url=a.url.rstrip("/") + "/api/v1", headers=headers, timeout=30) as c:
        sched = c.get("/scheduler").raise_for_status().json()
        print(f"policy {sched['policy']}, host-busy {'on' if sched['host_busy_enabled'] else 'off'}")

        def submit(i):
            t, p = mix[i % len(mix)]
            r = c.post("/jobs", json={"type": t, "params": p, "placement": "auto"})
            r.raise_for_status()
            return r.json()["job_id"]

        t0 = time.monotonic()
        with ThreadPoolExecutor(a.concurrency) as pool:
            ids = list(pool.map(submit, range(a.n)))
        print(f"submitted {len(ids)} jobs in {time.monotonic() - t0:.2f} s; waiting for them to finish")
        pending = set(ids)
        while pending and time.monotonic() - t0 < a.timeout:
            time.sleep(2)
            try:
                pending = {j for j in pending if c.get(f"/jobs/{j}").json()["state"] not in TERMINAL}
            except httpx.TransportError as e:  # controller restarting (AC-4.7): keep waiting
                print(f"  t={time.monotonic() - t0:5.0f}s controller unreachable ({type(e).__name__})", flush=True)
                continue
            print(f"  t={time.monotonic() - t0:5.0f}s not finished: {len(pending)}", flush=True)
        jobs = [c.get(f"/jobs/{j}").json() for j in ids]

    placed = Counter(j["worker_id"] for j in jobs)
    states = Counter(j["state"] for j in jobs)
    overflow = [j for j in jobs if j["overflow"]]
    reasons = Counter((w, r) for j in jobs for w, r in (j["decision"] or {}).get("ineligible", {}).items())
    print("\nplacement:", ", ".join(f"{w or 'none'}={n}" for w, n in sorted(placed.items(), key=str)))
    print("states:   ", ", ".join(f"{s}={n}" for s, n in sorted(states.items())))
    print(f"overflow (no eligible worker at first attempt): {len(overflow)}")
    print("ineligible reasons in the latest decision of each job:")
    for (w, r), n in sorted(reasons.items()):
        print(f"  {w:10s} {r:18s} {n}")
    first = jobs[0]["decision"]
    if first:
        print(f"example decision (job {jobs[0]['job_id'][:8]}): chose {first['worker_id']}, "
              f"eligible {first['eligible']}, ineligible {first['ineligible']}")
    print("\nnext: python -m experiments.audit   (no-overbooking check)")
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())
