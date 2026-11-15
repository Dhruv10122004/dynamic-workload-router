# scheduler/

Pure placement logic: which worker gets a job, or why none can. Implements plan §7
(interface §7.1, host-busy §7.2, policies §7.3). The controller builds the views and
persists the outcome; nothing here touches SQLite, the network or the clock.

## Public interface
- `views.py`: `WorkerView`, `JobView`, `Decision` (frozen dataclasses, plan §7.1);
  `TIER_RANK` (edge 0, fog 1, cloud 2).
- `eligibility.py`:
  - `REASONS`: the check order: stale, draining, unsupported_type, no_free_slot,
    mem_budget, mem_floor, host_busy, not_requested, reserved_for_aged (D-017).
  - `reason_for(job, w, host_busy_enabled=, held=)`: the first failing check, or None.
  - `eligibility(job, workers, ...) -> (eligible ids, {id: reason})`.
  - `structural_reason(job, w)`, `structurally_impossible(job, workers)`: the REJECTED
    rule (D-014).
- `hostbusy.py`: `HostBusy(high=70, low=50)`. `.add(foreign_cpu_pct) -> changed`;
  `.busy`, `.mean`, `.reset()`. Window 10, enters busy at ≥5 samples with mean ≥ high,
  leaves at mean ≤ low.
- `policies.py`: `make_policy(name, host_busy_enabled=)` → LocalFirst | EdgeOnly |
  FogOnly | RoundRobin. `.decide(job, workers, held=frozenset()) -> Decision`. `POLICIES`
  is the registry by name.

## Rules
- Pure: no I/O, no time, no randomness. Only RoundRobin keeps state (its cursor).
- Every policy goes through the shared eligibility filter; a policy only chooses among
  the eligible workers.
- On a structural reject, report permanent reasons, never transient ones (D-014).
- Adding a reason code means updating `REASONS` and the ordering test.

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_scheduler.py`
