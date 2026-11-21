import itertools

import pytest

from shared.states import (
    KILL_REASONS, NON_TERMINAL, TERMINAL, IllegalTransition, JobState as S, can_transition, transition,
)

# Plan §6.2 table, written out independently of shared/states.py.
EXPECTED = {
    (S.PENDING, S.ASSIGNED), (S.PENDING, S.REJECTED), (S.PENDING, S.CANCELLED), (S.PENDING, S.FAILED),
    *((S.ASSIGNED, t) for t in (S.ACCEPTED, S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.UNKNOWN)),
    *((S.ACCEPTED, t) for t in (S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.UNKNOWN)),
    *((S.RUNNING, t) for t in (S.SUCCEEDED, S.FAILED, S.KILLED, S.UNKNOWN)),
    *((S.UNKNOWN, t) for t in (S.ACCEPTED, S.RUNNING, S.SUCCEEDED, S.FAILED, S.KILLED, S.CANCELLED, S.ASSIGNED)),
}


def _reasons_for(src, dst):
    if dst is S.FAILED:
        return {"fail_reason": {S.PENDING: "queue_timeout", S.UNKNOWN: "lost"}.get(src, "exit_nonzero")}
    if dst is S.KILLED:
        return {"kill_reason": "cancel"}
    return {}


def test_state_sets_partition_all_ten_states():
    assert len(S) == 10
    assert NON_TERMINAL | TERMINAL == set(S) and not NON_TERMINAL & TERMINAL


@pytest.mark.parametrize("src,dst", list(itertools.product(S, S)), ids=lambda s: s.value)
def test_every_state_pair(src, dst):
    allowed = (src, dst) in EXPECTED
    assert can_transition(src, dst) is allowed
    if allowed:
        assert transition(src, dst, **_reasons_for(src, dst)) is dst
    else:
        with pytest.raises(IllegalTransition):
            transition(src, dst, **_reasons_for(src, dst))


@pytest.mark.parametrize("src", sorted(TERMINAL))
def test_terminal_states_are_immutable(src):
    assert not any(can_transition(src, dst) for dst in S)


def test_failed_requires_reason_valid_for_source():
    with pytest.raises(IllegalTransition):
        transition(S.RUNNING, S.FAILED)
    with pytest.raises(IllegalTransition):
        transition(S.PENDING, S.FAILED, fail_reason="exit_nonzero")  # only queue_timeout from PENDING
    with pytest.raises(IllegalTransition):
        transition(S.RUNNING, S.FAILED, fail_reason="lost")  # lost only from UNKNOWN
    assert transition(S.UNKNOWN, S.FAILED, fail_reason="agent_restart") is S.FAILED
    assert transition(S.ASSIGNED, S.FAILED, fail_reason="worker_validation") is S.FAILED


@pytest.mark.parametrize("reason", sorted(KILL_REASONS))
def test_killed_reasons(reason):
    assert transition(S.RUNNING, S.KILLED, kill_reason=reason) is S.KILLED


def test_reason_on_wrong_target_rejected():
    with pytest.raises(IllegalTransition):
        transition(S.RUNNING, S.KILLED, kill_reason="bogus")
    with pytest.raises(IllegalTransition):
        transition(S.RUNNING, S.SUCCEEDED, kill_reason="cancel")
    with pytest.raises(IllegalTransition):
        transition(S.RUNNING, S.SUCCEEDED, fail_reason="exit_nonzero")


def test_accepts_plain_strings():
    assert transition("PENDING", "ASSIGNED") is S.ASSIGNED
