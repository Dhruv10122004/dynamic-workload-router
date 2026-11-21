"""Workload catalog (plan §4): types, parameter bounds, declared memory, runtime caps.

Pure data. Validation of incoming parameters lives in shared/schemas.py (pydantic) and uses
these bounds; the controller serves this catalog at GET /catalog. `check_params` is the same
check without pydantic, for the job process itself, where importing pydantic would cost
~0.5 s per job (DECISIONS D-026); a test keeps the two in agreement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MIB = 1024 * 1024
BASE_MEM_MB = 48  # interpreter + workload overhead (whole process tree), declared for every type (D-019)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: type  # int or float
    min: float
    max: float
    default: float
    test_only: bool = False


@dataclass(frozen=True)
class WorkloadType:
    name: str
    params: tuple[ParamSpec, ...]
    description: str

    def declared_mem_mb(self, p: dict) -> int:
        if self.name == "data_digest":
            return BASE_MEM_MB + math.ceil(p["input_bytes"] / MIB)
        if self.name == "mem_touch":
            return BASE_MEM_MB + p["mem_mb"]
        return BASE_MEM_MB

    def max_runtime_s(self, p: dict) -> float:
        if self.name == "mem_touch":
            return float(p["hold_s"]) + 60.0
        return 120.0

    def to_json(self) -> dict:
        return {
            "type": self.name,
            "description": self.description,
            "params": [
                {"name": s.name, "kind": s.kind.__name__, "min": s.min, "max": s.max,
                 "default": s.default, "test_only": s.test_only}
                for s in self.params
            ],
        }


CATALOG: dict[str, WorkloadType] = {
    "cpu_hash": WorkloadType(
        "cpu_hash",
        (ParamSpec("iterations", int, 10_000, 5_000_000, 1_000_000),),
        "Chained SHA-256 loop; pure CPU.",
    ),
    "data_digest": WorkloadType(
        "data_digest",
        (ParamSpec("input_bytes", int, 0, 50 * MIB, 8 * MIB),
         ParamSpec("rounds", int, 1, 20, 3)),
        "Hash a controller-generated input blob `rounds` times.",
    ),
    "mem_touch": WorkloadType(
        "mem_touch",
        (ParamSpec("mem_mb", int, 16, 1024, 128),
         ParamSpec("hold_s", float, 0, 30, 5),
         ParamSpec("actual_mb_override", int, 16, 4096, 0, test_only=True)),
        "Allocate and touch mem_mb, then hold it for hold_s.",
    ),
}


def check_params(type_: str, params: object) -> dict:
    """Validate params like shared.schemas.validate_job (strict types, bounds, no unknown
    keys, defaults filled in, test-only params allowed) without importing pydantic.
    Raises ValueError with every problem."""
    wt = CATALOG.get(type_)
    if wt is None:
        raise ValueError(f"unknown workload type {type_!r}")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    specs = {s.name: s for s in wt.params}
    errors = [f"{k}: unknown parameter" for k in params if k not in specs]
    out: dict = {}
    for s in wt.params:
        if s.name not in params or (s.test_only and params[s.name] is None):
            if not s.test_only:
                out[s.name] = s.default
            continue
        v = params[s.name]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (s.kind is int and not isinstance(v, int)):
            errors.append(f"{s.name}: must be {s.kind.__name__}")
            continue
        if not (s.min <= v <= s.max) or (isinstance(v, float) and math.isnan(v)):
            errors.append(f"{s.name}: must be in [{s.min}, {s.max}]")
            continue
        out[s.name] = s.kind(v)
    if errors:
        raise ValueError("; ".join(errors))
    return out
