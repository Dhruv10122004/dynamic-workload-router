"""Seeded job traces (plan §12.2): CSV + sha256. Same arguments -> byte-identical file (AC-6.1).

    python -m experiments.gen_trace --reference experiments/reference.json --load 1.5 \
        --mix mixed --arrival poisson --duration 180 --seed 1 --out trace.csv

Load is relative to Edge's capacity, measured once by the reference timing run:
    capacity (jobs/s) = Edge slots / E[time a job holds an Edge slot, under the mix]
    arrival rate      = load x capacity
The slot time is `mean_service_s` (assignment to end, controller clock: dispatch and process
start included) by default, or `mean_exec_s` with --capacity-basis exec_s (DECISIONS D-026).
That is only how load levels are defined; the scheduler never sees these numbers.

Mixes: cpu, mem, data (one type each) and mixed (40% cpu / 30% mem / 30% data). Each type
draws its parameters uniformly from a small fixed set of sizes (VARIANTS), so job sizes vary
but stay inside what the reference run measured.
Arrivals: poisson (exponential gaps) or bursts (on/off square wave with the same mean rate).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from pathlib import Path

from shared.traces import (  # noqa: F401  (re-exported: the harness and tests import them from here)
    BASES, BURST_OFF_S, BURST_ON_S, MIB, MIXES, VARIANTS, capacity_jobs_per_s, generate, mean_time, variant_key,
)


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["index", "t_s", "type", "params_json"])
    for r in rows:
        w.writerow([r["index"], f"{r['t_s']:.6f}", r["type"], json.dumps(r["params"], sort_keys=True)])
    return buf.getvalue().encode("utf-8")


def read_trace(path: Path) -> tuple[list[dict], str]:
    data = path.read_bytes()
    rows = [{"index": int(r["index"]), "t_s": float(r["t_s"]), "type": r["type"], "params": json.loads(r["params_json"])}
            for r in csv.DictReader(io.StringIO(data.decode("utf-8")))]
    return rows, hashlib.sha256(data).hexdigest()


def write_trace(path: Path, rows: list[dict], meta: dict) -> str:
    """Write the CSV and a `.meta.json` beside it. Returns the CSV's sha256."""
    data = to_csv(rows)
    digest = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.with_suffix(".meta.json").write_text(json.dumps({**meta, "sha256": digest, "jobs": len(rows)}, indent=2),
                                              encoding="utf-8")
    return digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a seeded job trace (CSV + sha256)")
    ap.add_argument("--reference", type=Path, default=Path("experiments/reference.json"))
    ap.add_argument("--load", type=float, required=True, help="multiple of Edge capacity, e.g. 1.5")
    ap.add_argument("--mix", choices=sorted(MIXES), default="mixed")
    ap.add_argument("--arrival", choices=("poisson", "bursts"), default="poisson")
    ap.add_argument("--duration", type=float, default=180.0, help="seconds of arrivals")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--capacity-basis", choices=sorted(BASES), default="service")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    reference = json.loads(a.reference.read_text(encoding="utf-8"))
    rows = generate(reference=reference, load=a.load, mix=a.mix, arrival=a.arrival, duration_s=a.duration,
                    seed=a.seed, basis=a.capacity_basis)
    meta = {"seed": a.seed, "load": a.load, "mix": a.mix, "arrival": a.arrival, "duration_s": a.duration,
            "capacity_basis": a.capacity_basis,
            "rate_jobs_per_s": a.load * capacity_jobs_per_s(reference, a.mix, a.capacity_basis),
            "reference": {"path": str(a.reference), "edge_worker": reference["edge_worker"],
                          "edge_slots": reference["edge_slots"],
                          "mean_s": mean_time(reference, a.mix, a.capacity_basis)}}
    digest = write_trace(a.out, rows, meta)
    print(f"{a.out}: {len(rows)} jobs over {a.duration:.0f} s at {meta['rate_jobs_per_s']:.3f} jobs/s, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
