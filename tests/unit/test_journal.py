import time

import pytest

from shared.states import JobState
from worker.journal import PRUNE_AFTER_S, Journal, row_to_snapshot

SPEC = {"job_id": "j1", "type": "cpu_hash", "params": {"iterations": 10_000}}


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def test_duplicate_assignment_is_ignored(journal):
    assert journal.accept("j1", SPEC) is True
    assert journal.accept("j1", SPEC) is False
    assert journal.accept("j1", SPEC, cancelled=True) is False  # never rewrites an existing row
    assert journal.get("j1")["state"] == "ACCEPTED"


def test_cancel_in_same_reply_is_never_spawned(journal):
    journal.accept("j1", SPEC, cancelled=True)
    assert journal.get("j1")["state"] == "CANCELLED"
    assert journal.accepted_fifo() == []


def test_worker_validation_failure(journal):
    journal.accept("j1", SPEC, invalid_error="params.iterations: too small")
    row = journal.get("j1")
    assert (row["state"], row["fail_reason"]) == ("FAILED", "worker_validation")


def test_write_ahead_lifecycle_and_snapshot(journal):
    journal.accept("j1", SPEC)
    journal.accept("j2", SPEC)
    assert [r["job_id"] for r in journal.accepted_fifo()] == ["j1", "j2"]
    journal.mark_running("j1", slot=0, pid=123, pid_create_time=1.5, job_dir="d", spawn_s=0.01)
    journal.mark_terminal("j1", JobState.SUCCEEDED, exit_code=0, result={"exec_s": 1.0, "checksum": "ab"})
    snap = {s["job_id"]: s for s in map(row_to_snapshot, journal.snapshot_rows())}
    assert snap["j1"]["state"] == "SUCCEEDED" and snap["j1"]["result"]["checksum"] == "ab"
    assert snap["j1"]["pid"] == 123
    journal.ack_terminal(["j1", "j2"])  # j2 is not terminal: its ack is ignored
    assert [r["job_id"] for r in journal.snapshot_rows()] == ["j2"]


def test_illegal_journal_transition(journal):
    journal.accept("j1", SPEC)
    journal.mark_terminal("j1", JobState.CANCELLED)
    with pytest.raises(ValueError):
        journal.mark_running("j1", slot=0, pid=1, pid_create_time=1.0, job_dir="d", spawn_s=0.0)


def test_prune_only_acknowledged_old_rows(journal):
    journal.accept("old", SPEC)
    journal.mark_terminal("old", JobState.CANCELLED)
    journal.ack_terminal(["old"])
    journal.accept("live", SPEC)
    assert journal.prune(now=time.time()) == 0
    assert journal.prune(now=time.time() + PRUNE_AFTER_S + 1) == 1
    assert journal.get("old") is None and journal.get("live") is not None
