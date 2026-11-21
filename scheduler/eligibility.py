"""The one eligibility filter every policy uses (plan §7.1). The first failing check names the reason."""
from __future__ import annotations

from scheduler.views import JobView, WorkerView

# Plan §7.1 order. `host_busy` is skipped when the rule is disabled (§7.2 ablation).
# `reserved_for_aged` (DECISIONS D-017): the worker is held this pass for an older job that has
# waited longer than AGING_S and could run there.
REASONS = ("stale", "draining", "unsupported_type", "no_free_slot", "mem_budget", "mem_floor",
           "host_busy", "not_requested", "reserved_for_aged")


def reason_for(job: JobView, w: WorkerView, *, host_busy_enabled: bool = True,
               held: frozenset[str] = frozenset()) -> str | None:
    """Why `w` cannot take `job` right now, or None if it can."""
    if not w.fresh:
        return "stale"
    if w.draining:
        return "draining"
    if job.type not in w.supported_types:
        return "unsupported_type"
    if w.slots_used >= w.slots_total:
        return "no_free_slot"
    if w.mem_committed_mb + job.mem_mb > w.mem_budget_mb:
        return "mem_budget"
    if w.mem_available_mb - job.mem_mb < w.mem_floor_mb:
        return "mem_floor"
    if host_busy_enabled and w.host_busy:
        return "host_busy"
    if job.placement != "auto" and job.placement != w.worker_id:
        return "not_requested"
    if w.worker_id in held:
        return "reserved_for_aged"
    return None


def eligibility(job: JobView, workers: list[WorkerView], *, host_busy_enabled: bool = True,
                held: frozenset[str] = frozenset()) -> tuple[tuple[str, ...], dict[str, str]]:
    """(eligible worker ids sorted, {ineligible worker id: reason})."""
    eligible, ineligible = [], {}
    for w in sorted(workers, key=lambda w: w.worker_id):
        r = reason_for(job, w, host_busy_enabled=host_busy_enabled, held=held)
        if r is None:
            eligible.append(w.worker_id)
        else:
            ineligible[w.worker_id] = r
    return tuple(eligible), ineligible


def structural_reason(job: JobView, w: WorkerView) -> str | None:
    """Why `w` could never take `job`, whatever its load or freshness (plan §6.2), or None."""
    if job.placement != "auto" and job.placement != w.worker_id:
        return "not_requested"
    if job.type not in w.supported_types:
        return "unsupported_type"
    if job.mem_mb > w.mem_budget_mb:
        return "mem_budget"
    return None


def could_ever_run(job: JobView, w: WorkerView) -> bool:
    return structural_reason(job, w) is None


def structurally_impossible(job: JobView, workers: list[WorkerView]) -> bool:
    """No registered worker the job may use could ever run it (DECISIONS D-014).

    A pinned job considers only its named worker. With no registered worker at all
    nothing is known yet, so the job waits instead of being rejected."""
    if not workers:
        return False
    return not any(could_ever_run(job, w) for w in workers)
