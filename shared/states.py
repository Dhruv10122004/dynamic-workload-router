"""Job state machine (plan §6.2). Pure: no I/O.

`transition()` validates a change and returns the new state. The controller's
db.transition_job() is the only caller that persists a change; it calls this
first and writes the job_events row in the same transaction.
"""
from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    KILLED = "KILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


S = JobState
NON_TERMINAL = frozenset({S.PENDING, S.ASSIGNED, S.ACCEPTED, S.RUNNING, S.UNKNOWN})
TERMINAL = frozenset({S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.REJECTED})
# States a worker can report in its snapshot.
WORKER_REPORTED = frozenset({S.ACCEPTED, S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED})
# Jobs holding a worker reservation (plan §6.3).
COMMITTED = frozenset({S.ASSIGNED, S.ACCEPTED, S.RUNNING, S.UNKNOWN})

WORKER_FAIL_REASONS = frozenset(
    {"exit_nonzero", "spawn_error", "input_transfer_error", "worker_validation", "agent_restart"}
)
FAIL_REASONS = WORKER_FAIL_REASONS | {"queue_timeout", "lost"}
KILL_REASONS = frozenset({"cancel", "timeout", "max_runtime", "memory_limit"})

_ALLOWED: dict[JobState, frozenset[JobState]] = {
    S.PENDING: frozenset({S.ASSIGNED, S.REJECTED, S.CANCELLED, S.FAILED}),
    S.ASSIGNED: frozenset({S.ACCEPTED, S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.UNKNOWN}),
    S.ACCEPTED: frozenset({S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.UNKNOWN}),
    S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.KILLED, S.UNKNOWN}),
    S.UNKNOWN: frozenset({S.ACCEPTED, S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.ASSIGNED}),
    **{t: frozenset() for t in TERMINAL},
}

# Which fail_reason values are legal for FAILED, by source state.
_FAIL_REASONS_FROM: dict[JobState, frozenset[str]] = {
    S.PENDING: frozenset({"queue_timeout"}),
    S.ASSIGNED: WORKER_FAIL_REASONS,
    S.ACCEPTED: WORKER_FAIL_REASONS,
    S.RUNNING: WORKER_FAIL_REASONS,
    S.UNKNOWN: WORKER_FAIL_REASONS | {"lost"},
}


class IllegalTransition(ValueError):
    pass


def can_transition(src: JobState | str, dst: JobState | str) -> bool:
    return JobState(dst) in _ALLOWED[JobState(src)]


def transition(
    src: JobState | str,
    dst: JobState | str,
    *,
    fail_reason: str | None = None,
    kill_reason: str | None = None,
) -> JobState:
    """Validate src -> dst (and its reason) and return dst. Raises IllegalTransition."""
    src, dst = JobState(src), JobState(dst)
    if dst not in _ALLOWED[src]:
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed")
    if dst is S.FAILED:
        if fail_reason not in _FAIL_REASONS_FROM[src]:
            raise IllegalTransition(f"{src.value} -> FAILED needs fail_reason in "
                                    f"{sorted(_FAIL_REASONS_FROM[src])}, got {fail_reason!r}")
    elif fail_reason is not None:
        raise IllegalTransition(f"fail_reason only applies to FAILED, got {dst.value}")
    if dst is S.KILLED:
        if kill_reason not in KILL_REASONS:
            raise IllegalTransition(f"KILLED needs kill_reason in {sorted(KILL_REASONS)}, got {kill_reason!r}")
    elif kill_reason is not None:
        raise IllegalTransition(f"kill_reason only applies to KILLED, got {dst.value}")
    return dst


def is_terminal(state: JobState | str) -> bool:
    return JobState(state) in TERMINAL
