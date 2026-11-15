"""Inputs and output of a placement decision (plan §7.1). Frozen: a policy cannot mutate them."""
from __future__ import annotations

from dataclasses import dataclass, field

TIER_RANK = {"edge": 0, "fog": 1, "cloud": 2}


@dataclass(frozen=True)
class WorkerView:
    """Built by the controller from SQLite (slots/memory derived from committed jobs)
    plus the latest heartbeat (mem_available_mb) and the host-busy tracker."""
    worker_id: str
    tier: str
    fresh: bool
    draining: bool
    host_busy: bool
    supported_types: frozenset[str]
    slots_total: int
    slots_used: int
    mem_budget_mb: int
    mem_committed_mb: int
    mem_available_mb: int
    mem_floor_mb: int


@dataclass(frozen=True)
class JobView:
    job_id: str
    type: str
    mem_mb: int
    placement: str  # "auto" | worker_id


@dataclass(frozen=True)
class Decision:
    worker_id: str | None          # None -> stays PENDING (or REJECTED if structural_reject)
    policy: str
    eligible: tuple[str, ...]
    ineligible: dict[str, str] = field(default_factory=dict)  # worker_id -> reason code
    structural_reject: bool = False
