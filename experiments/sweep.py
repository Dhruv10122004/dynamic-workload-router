"""Run a block of experiments unattended (plan §12.1-12.3, M6).

    python -m experiments.sweep experiments/configs/sweep.json [--dry-run]

For every repetition r and load L, one trace (seed = base_seed + 100*r + load index) is
generated and replayed once per strategy, so every strategy sees the same files. The
strategy order rotates by one each repetition. Resumable: a (strategy, load, rep) whose run
directory already has a manifest is skipped, so an interrupted sweep continues where it stopped.
`index.json` in the block directory maps each key to its run directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from experiments.common import RESULTS, client, log
from experiments.gen_trace import capacity_jobs_per_s, generate, write_trace
from experiments.replay import run_trace


def plan_runs(cfg: dict) -> list[dict]:
    strategies = cfg["strategies"]
    out = []
    for rep in range(cfg["reps"]):
        order = strategies[rep % len(strategies):] + strategies[:rep % len(strategies)]
        for li, load in enumerate(cfg["loads"]):
            seed = cfg["base_seed"] + 100 * rep + li
            for s in order:
                out.append({"key": f"{s['name']}|load={load}|rep={rep}", "strategy": s, "load": load, "rep": rep,
                            "seed": seed})
    return out


def trace_path(block_dir: Path, cfg: dict, load: float, seed: int) -> Path:
    return block_dir / "traces" / f"{cfg['mix']}-{cfg['arrival']}-L{load}-s{seed}.csv"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Unattended experiment block")
    ap.add_argument("config", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the trace sizes only")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--admin-token")
    ap.add_argument("--results", type=Path, default=RESULTS)
    a = ap.parse_args(argv)
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    reference = json.loads(Path(cfg["reference"]).read_text(encoding="utf-8"))
    block_dir = a.results / cfg["block"]
    index_path = block_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    runs = plan_runs(cfg)
    basis = cfg.get("capacity_basis", "service")
    per_run = cfg["duration_s"] + cfg["drain_s"]
    print(f"block {cfg['block']}: {len(runs)} runs, each <= {per_run:.0f} s + idle wait "
          f"(about {len(runs) * (per_run + 45) / 3600:.1f} h); capacity "
          f"{capacity_jobs_per_s(reference, cfg['mix'], basis):.3f} jobs/s at load 1 (basis {basis})")

    # Traces first, so a dry run shows exactly what will be replayed.
    for r in runs:
        tp = trace_path(block_dir, cfg, r["load"], r["seed"])
        if not tp.is_file():
            rows = generate(reference=reference, load=r["load"], mix=cfg["mix"], arrival=cfg["arrival"],
                            duration_s=cfg["duration_s"], seed=r["seed"], basis=basis)
            write_trace(tp, rows, {"seed": r["seed"], "load": r["load"], "mix": cfg["mix"], "arrival": cfg["arrival"],
                                   "duration_s": cfg["duration_s"], "reference": cfg["reference"],
                                   "capacity_basis": basis,
                                   "rate_jobs_per_s": r["load"] * capacity_jobs_per_s(reference, cfg["mix"], basis)})
        r["trace"] = tp
    if a.dry_run:
        for r in runs:
            done = "done" if r["key"] in index else ""
            print(f"  {r['key']:40s} {r['trace'].name} {done}")
        return 0

    with client(a.url, a.admin_token) as c:
        for i, r in enumerate(runs, 1):
            if r["key"] in index and (Path(index[r["key"]]) / "manifest.json").is_file():
                continue
            s = r["strategy"]
            log(f"=== {i}/{len(runs)} {r['key']}")
            extra = {"sweep": {"key": r["key"], "strategy": s["name"], "load": r["load"], "rep": r["rep"],
                               "config_file": str(a.config)}}
            if s.get("stress"):
                extra["stress"] = s["stress"]
            run_dir = run_trace(c, trace=r["trace"], policy=s["policy"], host_busy=s.get("host_busy", True),
                                block=cfg["block"], name=r["key"], drain_s=cfg["drain_s"],
                                queue_timeout_s=cfg.get("queue_timeout_s", 300.0), require=cfg.get("require"),
                                network=cfg.get("network", "unknown"), extra_config=extra, out_root=a.results,
                                idle_cpu=cfg.get("idle_cpu_pct", 10.0), idle_s=cfg.get("idle_quiet_s", 30.0))
            index[r["key"]] = str(run_dir)
            index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\nblock {cfg['block']} complete: python -m experiments.analyze {block_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
