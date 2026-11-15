"""V1 placement policies (plan §7.3). Each chooses among the workers the shared filter allows."""
from __future__ import annotations

from typing import Protocol

from scheduler.eligibility import eligibility, structural_reason, structurally_impossible
from scheduler.views import TIER_RANK, Decision, JobView, WorkerView


class Scheduler(Protocol):
    name: str

    def decide(self, job: JobView, workers: list[WorkerView], held: frozenset[str] = frozenset()) -> Decision: ...


class _Base:
    name = ""

    def __init__(self, *, host_busy_enabled: bool = True):
        self.host_busy_enabled = host_busy_enabled

    def decide(self, job: JobView, workers: list[WorkerView], held: frozenset[str] = frozenset()) -> Decision:
        """`held`: workers reserved this pass for an aged job (DECISIONS D-017)."""
        if structurally_impossible(job, workers):
            # Report the permanent reason per worker, not a transient one such as `stale`.
            reasons = {w.worker_id: structural_reason(job, w) for w in sorted(workers, key=lambda w: w.worker_id)}
            return Decision(None, self.name, (), reasons, structural_reject=True)
        eligible, ineligible = eligibility(job, workers, host_busy_enabled=self.host_busy_enabled, held=held)
        by_id = {w.worker_id: w for w in workers}
        chosen = self.choose([by_id[i] for i in eligible]) if eligible else None
        return Decision(chosen, self.name, eligible, ineligible)

    def choose(self, eligible: list[WorkerView]) -> str | None:
        raise NotImplementedError


def _rank(w: WorkerView) -> tuple[int, str]:
    return TIER_RANK.get(w.tier, len(TIER_RANK)), w.worker_id


class LocalFirst(_Base):
    """Lowest tier rank (edge, fog, cloud), then worker_id. The proposed system."""
    name = "LocalFirst"

    def choose(self, eligible):
        return min(eligible, key=_rank).worker_id


class _TierOnly(_Base):
    tier = ""

    def choose(self, eligible):
        mine = [w for w in eligible if w.tier == self.tier]
        return min(mine, key=_rank).worker_id if mine else None


class EdgeOnly(_TierOnly):
    """Baseline: one device, no offloading. Jobs Edge can't take now stay PENDING (overflow)."""
    name, tier = "EdgeOnly", "edge"


class FogOnly(_TierOnly):
    """Sanity baseline."""
    name, tier = "FogOnly", "fog"


class RoundRobin(_Base):
    """Cycle through the eligible workers in worker_id order, starting after the last choice."""
    name = "RoundRobin"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.last: str | None = None

    def choose(self, eligible):
        ids = sorted(w.worker_id for w in eligible)
        after = [i for i in ids if self.last is None or i > self.last]
        self.last = (after or ids)[0]
        return self.last


POLICIES: dict[str, type[_Base]] = {p.name: p for p in (LocalFirst, EdgeOnly, FogOnly, RoundRobin)}


def make_policy(name: str, *, host_busy_enabled: bool = True) -> _Base:
    try:
        return POLICIES[name](host_busy_enabled=host_busy_enabled)
    except KeyError:
        raise ValueError(f"unknown policy {name!r}; choose one of {sorted(POLICIES)}") from None
