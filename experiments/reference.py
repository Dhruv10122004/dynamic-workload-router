"""Reference timing run (plan §12.2): per job size on Edge, mean exec_s and mean service time
(assignment to end on the controller clock: how long the job holds an Edge slot, D-026).

    python -m experiments.reference [--reps 8] [--out experiments/reference.json]

Runs every VARIANT of gen_trace `--reps` times as one tagged run under EdgeOnly, so Edge
runs them with all its slots busy (the capacity the load levels are defined against).
The result only defines load levels; the scheduler never uses it.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

from experiments.common import RESULTS, client, workers_snapshot
from experiments.gen_trace import VARIANTS, variant_key, write_trace
from experiments.replay import run_trace


def reference_rows(reps: int) -> list[dict]:
    """Every variant `reps` times, interleaved (so no size runs only cold or only warm)."""
    rows = []
    for _ in range(reps):
        for type_ in sorted(VARIANTS):
            for params in VARIANTS[type_]:
                rows.append({"index": len(rows), "t_s": 0.0, "type": type_, "params": params})
    return rows


def summarize(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    edge = [w for w in manifest["run"]["config"]["workers"] if w["tier"] == "edge"]
    if len(edge) != 1:
        raise SystemExit(f"expected exactly one edge worker, found {[w['worker_id'] for w in edge]}")
    times: dict[str, list[float]] = {}
    service: dict[str, list[float]] = {}
    with open(run_dir / "jobs.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["state"] == "SUCCEEDED" and r["worker_id"] == edge[0]["worker_id"]:
                k = variant_key(r["type"], json.loads(r["params_json"]))
                times.setdefault(k, []).append(float(r["exec_s"]))
                service.setdefault(k, []).append(float(r["end_ts"]) - float(r["assign_ts"]))
    missing = [variant_key(t, p) for t in VARIANTS for p in VARIANTS[t] if variant_key(t, p) not in times]
    if missing:
        raise SystemExit(f"no successful Edge run for {missing}; see {run_dir / 'jobs.csv'}")
    return {
        "edge_worker": edge[0]["worker_id"], "edge_slots": edge[0]["slots_total"],
        "edge_cores": [s["cpus"] for s in edge[0]["slots"]], "hostname": edge[0]["hostname"],
        "mean_exec_s": {k: statistics.mean(v) for k, v in sorted(times.items())},
        "stdev_exec_s": {k: statistics.stdev(v) if len(v) > 1 else 0.0 for k, v in sorted(times.items())},
        "mean_service_s": {k: statistics.mean(v) for k, v in sorted(service.items())},
        "n": {k: len(v) for k, v in sorted(times.items())},
        "run_id": manifest["run"]["run_id"], "run_dir": str(run_dir), "git_commit": manifest["git_commit"],
        "harness_power": manifest["run"]["config"].get("harness_power"),
        "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reference timing run on Edge")
    ap.add_argument("--reps", type=int, default=8)
    ap.add_argument("--out", type=Path, default=Path("experiments/reference.json"))
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin-token")
    ap.add_argument("--network", default="unknown")
    ap.add_argument("--idle-cpu", type=float, default=10.0, help="start only when host CPU %% is below this")
    ap.add_argument("--idle-s", type=float, default=30.0)
    a = ap.parse_args(argv)
    trace = a.results / "reference" / "traces" / f"reference-x{a.reps}.csv"
    write_trace(trace, reference_rows(a.reps), {"kind": "reference", "reps": a.reps, "duration_s": 0.0})
    with client(a.url, a.admin_token) as c:
        edge = [w["worker_id"] for w in workers_snapshot(c) if w["tier"] == "edge"]
        run_dir = run_trace(c, trace=trace, policy="EdgeOnly", host_busy=False, block="reference",
                            name=f"reference-x{a.reps}", drain_s=600.0, queue_timeout_s=900.0, require=edge,
                            network=a.network, out_root=a.results, idle_cpu=a.idle_cpu, idle_s=a.idle_s)
    ref = summarize(run_dir)
    a.out.write_text(json.dumps(ref, indent=2), encoding="utf-8")
    print(f"wrote {a.out}")
    for k, v in ref["mean_exec_s"].items():
        print(f"  {k:55s} exec {v:6.3f} s (sd {ref['stdev_exec_s'][k]:.3f})  slot {ref['mean_service_s'][k]:6.3f} s"
              f"  n={ref['n'][k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
