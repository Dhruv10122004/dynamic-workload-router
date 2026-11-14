import time

import pytest

from worker.journal import Journal
from worker.supervisor import Supervisor


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def _spec(job_id, iterations):
    return {"job_id": job_id, "type": "cpu_hash", "params": {"iterations": iterations}}


def _run_until(sup, journal, job_ids, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sup.tick()
        if all(journal.get(j)["state"] not in ("ACCEPTED", "RUNNING") for j in job_ids):
            return
        time.sleep(0.02)
    raise AssertionError("jobs did not finish")


def test_spawn_wait_result(journal, tmp_path):
    notified = []
    sup = Supervisor(journal, [[0]], tmp_path / "jobs", on_terminal=lambda: notified.append(1))
    journal.accept("j1", _spec("j1", 20_000))
    sup.tick()
    row = journal.get("j1")
    assert row["state"] == "RUNNING" and row["pid"] > 0 and row["pid_create_time"] and row["spawn_s"] >= 0
    _run_until(sup, journal, ["j1"])
    row = journal.get("j1")
    assert row["state"] == "SUCCEEDED" and row["exit_code"] == 0 and '"checksum"' in row["result_json"]
    assert notified


def test_nonzero_exit_is_failed_with_stderr(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    journal.accept("bad", _spec("bad", 5))  # bypasses validation; workload exits 2
    _run_until(sup, journal, ["bad"])
    row = journal.get("bad")
    assert (row["state"], row["fail_reason"], row["exit_code"]) == ("FAILED", "exit_nonzero", 2)
    assert "invalid parameters" in row["error"]


def test_never_more_than_configured_slots(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    for j in ("a", "b", "c"):
        journal.accept(j, _spec(j, 200_000))
    end = time.monotonic() + 60
    while time.monotonic() < end:
        sup.tick()
        states = [journal.get(j)["state"] for j in ("a", "b", "c")]
        assert states.count("RUNNING") <= 1
        assert sup.free_slots() >= 0
        if all(s == "SUCCEEDED" for s in states):
            break
        time.sleep(0.01)
    assert [journal.get(j)["state"] for j in ("a", "b", "c")] == ["SUCCEEDED"] * 3


def test_cancel_before_spawn(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    journal.accept("j1", _spec("j1", 20_000))
    sup.request_cancel("j1")
    sup.tick()
    assert journal.get("j1")["state"] == "CANCELLED" and journal.get("j1")["pid"] is None
