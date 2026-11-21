"""Replay one trace as one tagged run (plan §12.3, §12.7).

    python -m experiments.replay --trace t.csv --policy LocalFirst --block pilot \
        [--host-busy on|off] [--drain 120] [--queue-timeout 300] [--network LAN] [--stress-at 60 --stress-for 120]

1. Wait until idle: no non-terminal job, required workers fresh, host CPU < 10% for 30 s.
2. PUT /scheduler (policy, host-busy, queue_timeout_s), POST /runs.
3. Open-loop: submit job i at start + t_i, whatever has completed (a thread pool keeps a
   slow request from delaying the schedule; the lag is recorded).
4. Wait for every job to finish, at most trace duration + drain. Then cancel what is left
   (those count as not completed), finish the run.
5. Export jobs/decisions/telemetry/events CSV + manifest.json to
   experiments/results/<block>/<run_id>/.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from experiments.common import (
    NON_TERMINAL, RESULTS, client, git_commit, log, median, percentile, power_state, wait_idle, workers_snapshot,
)
from experiments.gen_trace import read_trace

EXPORTS = ("jobs", "decisions", "telemetry", "events")


def _submit_all(c: httpx.Client, rows: list[dict], run_id: str, log) -> tuple[list[str | None], list[float], float]:
    """Open-loop submission. Returns (job ids by trace index, lag per job (s), start monotonic)."""
    ids: list[str | None] = [None] * len(rows)
    lags: list[float] = [0.0] * len(rows)
    errors: list[str] = []

    def one(r: dict, due: float) -> None:
        lags[r["index"]] = time.monotonic() - due
        try:
            resp = c.post("/jobs", json={"type": r["type"], "params": r["params"], "placement": "auto",
                                         "run_id": run_id, "trace_index": r["index"]})
            resp.raise_for_status()
            ids[r["index"]] = resp.json()["job_id"]
        except httpx.HTTPError as e:
            errors.append(f"#{r['index']}: {e}")

    start = time.monotonic()
    with ThreadPoolExecutor(16) as pool:
        for r in rows:
            due = start + r["t_s"]
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            pool.submit(one, r, due)
    for e in errors[:10]:
        log(f"submit failed {e}")
    return ids, lags, start


def _wait_done(c: httpx.Client, run_id: str, deadline: float, log) -> tuple[list[dict], dict[str, list[float]]]:
    """Poll the run's jobs until all are terminal or the deadline passes.
    Returns the last job listing and the RTT samples seen per worker."""
    rtts: dict[str, list[float]] = {}
    last_log = 0.0
    jobs: list[dict] = []
    while True:
        try:
            jobs = c.get("/jobs", params={"run_id": run_id, "limit": 5000}).raise_for_status().json()
            for w in c.get("/workers").raise_for_status().json():
                rtt = (w.get("telemetry") or {}).get("rtt_ms_last")
                if w["fresh"] and rtt is not None:
                    rtts.setdefault(w["worker_id"], []).append(rtt)
        except httpx.TransportError as e:
            log(f"controller unreachable ({type(e).__name__}); still waiting")
            time.sleep(2)
            continue
        left = [j for j in jobs if j["state"] in NON_TERMINAL]
        if not left or time.monotonic() >= deadline:
            return jobs, rtts
        if time.monotonic() - last_log > 15:
            log(f"  {len(left)} of {len(jobs)} jobs not finished")
            last_log = time.monotonic()
        time.sleep(2)


def _reference(trace_meta: dict) -> dict | None:
    ref = trace_meta.get("reference")
    path = ref.get("path") if isinstance(ref, dict) else ref
    if not path or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"path": str(path), "edge_worker": data.get("edge_worker"), "edge_slots": data.get("edge_slots"),
            "mean_exec_s": data.get("mean_exec_s")}


def run_trace(c: httpx.Client, *, trace: Path, policy: str, host_busy: bool, block: str, name: str | None = None,
              drain_s: float = 120.0, queue_timeout_s: float = 300.0, require: list[str] | None = None,
              network: str = "unknown", extra_config: dict | None = None, out_root: Path = RESULTS,
              idle_cpu: float = 10.0, idle_s: float = 30.0, log=log) -> Path:
    rows, trace_hash = read_trace(trace)
    meta_path = trace.with_suffix(".meta.json")
    trace_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    duration_s = float(trace_meta.get("duration_s") or (rows[-1]["t_s"] if rows else 0.0))
    require = require if require is not None else [w["worker_id"] for w in workers_snapshot(c)]

    log(f"[{block}] {name or trace.stem} {policy} host-busy={'on' if host_busy else 'off'}: "
        f"{len(rows)} jobs over {duration_s:.0f} s; waiting for idle")
    wait_idle(c, require=require, cpu_below=idle_cpu, quiet_s=idle_s, log=log)
    c.put("/scheduler", json={"policy": policy, "host_busy_enabled": host_busy,
                              "queue_timeout_s": queue_timeout_s}).raise_for_status()
    workers = workers_snapshot(c)
    config = {"block": block, "trace": str(trace), "trace_meta": trace_meta, "drain_s": drain_s,
              "idle_rule": {"cpu_below_pct": idle_cpu, "quiet_s": idle_s},
              "network": network, "require": require, "workers": workers, "harness_host": platform.node(),
              "harness_power": power_state(),
              **(extra_config or {})}
    run = c.post("/runs", json={"name": name or trace.stem, "trace_hash": trace_hash, "git_commit": git_commit(),
                                "config": config}).raise_for_status().json()
    run_id = run["run_id"]
    log(f"run {run_id} started")
    if config["harness_power"]["plugged_in"] is False:
        log("  WARNING: this laptop is on battery; timings differ from mains power (recorded in the manifest)")
    if config.get("stress"):
        log(f"  stress expected on the Fog host at +{config['stress']['at_s']} s for {config['stress']['for_s']} s")

    t_wall0 = time.time()
    ids, lags, start = _submit_all(c, rows, run_id, log)
    log(f"  all {len(rows)} submitted; max submit lag {max(lags, default=0) * 1000:.0f} ms")
    jobs, rtts = _wait_done(c, run_id, start + duration_s + drain_s, log)
    left = [j["job_id"] for j in jobs if j["state"] in NON_TERMINAL]
    if left:
        log(f"  drain over: cancelling {len(left)} unfinished job(s) (they count as not completed)")
        for j in left:
            try:
                c.post(f"/jobs/{j}/cancel")
            except httpx.HTTPError:
                pass
        _wait_done(c, run_id, time.monotonic() + 30, log)
    c.post(f"/runs/{run_id}/finish").raise_for_status()

    out = out_root / block / run_id
    out.mkdir(parents=True, exist_ok=True)
    for table in EXPORTS:
        r = c.get(f"/runs/{run_id}/export", params={"table": table}).raise_for_status()
        (out / f"{table}.csv").write_text(r.text, encoding="utf-8", newline="")
    manifest = {
        "run": c.get(f"/runs/{run_id}").raise_for_status().json(),
        "trace": {"path": str(trace), "sha256": trace_hash, "jobs": len(rows), "duration_s": duration_s, **trace_meta},
        "strategy": {"policy": policy, "host_busy_enabled": host_busy},
        "run_length_s": duration_s + drain_s,
        "wall_start": t_wall0,
        "submitted": sum(1 for i in ids if i), "submit_errors": sum(1 for i in ids if not i),
        "submit_lag_ms": {"p50": (percentile(lags, 50) or 0) * 1000, "p99": (percentile(lags, 99) or 0) * 1000,
                          "max": max(lags, default=0) * 1000},
        "cancelled_at_drain": left,
        "median_rtt_ms": {w: median(v) for w, v in rtts.items()},
        "network": network,
        "git_commit": run["git_commit"],
        # Edge timings per job size, so analyze.py can estimate the demand of overflow jobs that
        # never ran (EdgeOnly) from this directory alone (DECISIONS D-025).
        "reference": _reference(trace_meta),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"run {run_id} done -> {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay a trace as one tagged run")
    ap.add_argument("--trace", type=Path, required=True)
    ap.add_argument("--policy", default="LocalFirst")
    ap.add_argument("--host-busy", choices=("on", "off"), default="on")
    ap.add_argument("--block", default="adhoc", help="results/<block>/<run_id>/")
    ap.add_argument("--name")
    ap.add_argument("--drain", type=float, default=120.0)
    ap.add_argument("--queue-timeout", type=float, default=300.0)
    ap.add_argument("--require", help="comma list of workers that must be fresh (default: all registered)")
    ap.add_argument("--network", default="unknown", help="e.g. LAN, hotspot, tailscale-direct (recorded)")
    ap.add_argument("--stress-at", type=float, help="ablation: stress.py --follow-runs starts here (s)")
    ap.add_argument("--stress-for", type=float, default=120.0)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin-token")
    ap.add_argument("--out", type=Path, default=RESULTS)
    ap.add_argument("--idle-cpu", type=float, default=10.0, help="between runs: host CPU %% below this (plan §12.3)")
    ap.add_argument("--idle-s", type=float, default=30.0, help="...continuously for this long")
    a = ap.parse_args(argv)
    extra = {"stress": {"at_s": a.stress_at, "for_s": a.stress_for}} if a.stress_at is not None else {}
    with client(a.url, a.admin_token) as c:
        run_trace(c, trace=a.trace, policy=a.policy, host_busy=a.host_busy == "on", block=a.block, name=a.name,
                  drain_s=a.drain, queue_timeout_s=a.queue_timeout, network=a.network, extra_config=extra,
                  require=a.require.split(",") if a.require else None, out_root=a.out,
                  idle_cpu=a.idle_cpu, idle_s=a.idle_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
