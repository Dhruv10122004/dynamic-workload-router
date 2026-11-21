"""Job sizes, mixes and seeded arrival generation (plan §12.2), shared by the experiment
harness (experiments/gen_trace.py) and the classroom playground (controller/playground.py).

Pure: no I/O. Load is relative to Edge's capacity from the reference timing run:
capacity (jobs/s) = Edge slots / E[seconds a job holds an Edge slot under the mix] (D-026).
"""
from __future__ import annotations

import json
import math
import random

MIB = 1024 * 1024

# Job sizes per type. The reference run measures every variant on Edge.
VARIANTS: dict[str, list[dict]] = {
    "cpu_hash": [{"iterations": 500_000}, {"iterations": 1_000_000}, {"iterations": 2_000_000}],
    "mem_touch": [{"mem_mb": 64, "hold_s": 1.0}, {"mem_mb": 128, "hold_s": 2.0}, {"mem_mb": 256, "hold_s": 3.0}],
    "data_digest": [{"input_bytes": 4 * MIB, "rounds": 2}, {"input_bytes": 8 * MIB, "rounds": 2},
                    {"input_bytes": 16 * MIB, "rounds": 2}],
}
MIXES: dict[str, dict[str, float]] = {
    "cpu": {"cpu_hash": 1.0},
    "mem": {"mem_touch": 1.0},
    "data": {"data_digest": 1.0},
    "mixed": {"cpu_hash": 0.4, "mem_touch": 0.3, "data_digest": 0.3},
}
BURST_ON_S = 20.0
BURST_OFF_S = 20.0


def variant_key(type_: str, params: dict) -> str:
    """Canonical id of a job size, shared by the reference file and the traces."""
    return f"{type_}:{json.dumps(params, sort_keys=True, separators=(',', ':'))}"


BASES = {"service": "mean_service_s", "exec_s": "mean_exec_s"}


def mean_time(reference: dict, mix: str, basis: str = "service") -> float:
    """E[seconds per job on Edge] under the mix: types weighted by the mix, variants uniformly."""
    table = reference[BASES[basis]]
    total = 0.0
    for type_, w in MIXES[mix].items():
        times = [table[variant_key(type_, p)] for p in VARIANTS[type_]]
        total += w * sum(times) / len(times)
    return total


def capacity_jobs_per_s(reference: dict, mix: str, basis: str = "service") -> float:
    return reference["edge_slots"] / mean_time(reference, mix, basis)


def generate(*, reference: dict, load: float, mix: str, arrival: str, duration_s: float, seed: int,
             basis: str = "service") -> list[dict]:
    rng = random.Random(seed)
    rate = load * capacity_jobs_per_s(reference, mix, basis)
    types, weights = zip(*sorted(MIXES[mix].items()))
    if arrival not in ("poisson", "bursts"):
        raise ValueError(f"unknown arrival {arrival!r}")
    rows, tau = [], 0.0
    period = BURST_ON_S + BURST_OFF_S
    on_rate = rate * period / BURST_ON_S   # same mean rate as poisson over whole periods
    while True:
        if arrival == "poisson":
            tau += rng.expovariate(rate)
            t = tau
        else:
            # Poisson on an "on-time" clock, mapped onto [0, ON) of each period.
            tau += rng.expovariate(on_rate)
            t = math.floor(tau / BURST_ON_S) * period + tau % BURST_ON_S
        if t >= duration_s:
            break
        type_ = rng.choices(types, weights=weights)[0]
        params = rng.choice(VARIANTS[type_])
        rows.append({"index": len(rows), "t_s": round(t, 6), "type": type_, "params": params})
    return rows
