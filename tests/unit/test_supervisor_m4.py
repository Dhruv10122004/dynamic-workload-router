"""M4 worker reliability: cancel/kill with verification, timers, RSS watchdog, stuck slots,
restart recovery, spawn log (plan §5.4, §5.5; AC-4.1, 4.2, 4.3, 4.5, 4.6)."""
import json
import subprocess
import sys
import time

import psutil
import pytest

from worker import proctree, supervisor as sup_mod
from worker.journal import Journal
from worker.recovery import recover
from worker.supervisor import Supervisor


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def spec(type_="cpu_hash", params=None, mem_mb=48, max_runtime_s=120.0, timeout_s=None):
    params = params if params is not None else {"iterations": 5_000_000}
    return {"type": type_, "params": params, "mem_mb": mem_mb, "max_runtime_s": max_runtime_s,
            "timeout_s": timeout_s}


def tick_until(sup, pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sup.tick()
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def start(sup, journal, job_id, s):
    journal.accept(job_id, s)
    tick_until(sup, lambda: journal.get(job_id)["state"] == "RUNNING")
    row = journal.get(job_id)
    root = psutil.Process(row["pid"])
    tick_until(sup, lambda: len(proctree.tree(root)) >= 1, 5)
    time.sleep(0.3)  # let the interpreter start (Windows launcher -> child)
    return root, proctree.tree(root)


def gone(procs):
    return not any(proctree.alive(p) for p in procs)


# ---------------------------------------------------------------- AC-4.1 cancel

def test_cancel_running_cooperative(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    root, procs = start(sup, journal, "c1", spec())
    sup.request_cancel("c1")
    sup.request_cancel("c1")  # repeated (level-triggered replies): idempotent
    tick_until(sup, lambda: journal.get("c1")["state"] == "KILLED")
    row = journal.get("c1")
    assert row["kill_reason"] == "cancel" and row["termination_verified"] == 1
    assert row["exit_code"] == 3                     # the workload saw the sentinel
    assert 0 < row["kill_latency_ms"] < sup_mod.TERM_GRACE_S * 1000
    assert gone(procs) and sup.free_slots() == 1


def test_hard_kill_after_grace(journal, tmp_path, monkeypatch):
    monkeypatch.setattr(sup_mod, "TERM_GRACE_S", 0.0)  # kill before the workload can react
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    root, procs = start(sup, journal, "k1", spec())
    sup.request_cancel("k1")
    tick_until(sup, lambda: journal.get("k1")["state"] == "KILLED")
    row = journal.get("k1")
    assert row["kill_reason"] == "cancel" and row["termination_verified"] == 1 and row["exit_code"] != 0
    assert row["kill_latency_ms"] is not None and gone(procs)


# ---------------------------------------------------------------- AC-4.2 timers

def test_timeout_kills(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    start(sup, journal, "t1", spec(timeout_s=0.5))
    tick_until(sup, lambda: journal.get("t1")["state"] == "KILLED")
    row = journal.get("t1")
    assert row["kill_reason"] == "timeout" and row["termination_verified"] == 1
    assert "timeout_s" in row["error"]


def test_max_runtime_kills(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    start(sup, journal, "m1", spec(max_runtime_s=0.5))
    tick_until(sup, lambda: journal.get("m1")["state"] == "KILLED")
    assert journal.get("m1")["kill_reason"] == "max_runtime"


# ---------------------------------------------------------------- AC-4.3 RSS watchdog

def test_rss_watchdog_kills_overallocation(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    # Declares 48 + 16 = 64 MB but allocates 400 MB (test-mode override).
    s = spec("mem_touch", {"mem_mb": 16, "hold_s": 20, "actual_mb_override": 400}, mem_mb=64)
    journal.accept("w1", s)
    tick_until(sup, lambda: journal.get("w1")["state"] == "KILLED")
    row = journal.get("w1")
    assert row["kill_reason"] == "memory_limit" and row["termination_verified"] == 1
    assert "110% of declared 64 MB" in row["error"]


def test_watchdog_leaves_honest_jobs_alone(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    journal.accept("ok", spec("mem_touch", {"mem_mb": 64, "hold_s": 1}, mem_mb=48 + 64))
    tick_until(sup, lambda: journal.get("ok")["state"] not in ("ACCEPTED", "RUNNING"))
    assert journal.get("ok")["state"] == "SUCCEEDED"


# ---------------------------------------------------------------- stuck slot

def test_unverified_kill_marks_slot_stuck_until_gone(journal, tmp_path, monkeypatch):
    monkeypatch.setattr(sup_mod, "TERM_GRACE_S", 0.0)
    monkeypatch.setattr(sup_mod, "VERIFY_TIMEOUT_S", 0.3)
    real_kill = proctree.kill
    monkeypatch.setattr(proctree, "kill", lambda procs: None)  # the kill "fails"
    monkeypatch.setattr(sup_mod.Path, "touch", lambda self, *a, **k: None)  # and no sentinel: it never exits
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    root, procs = start(sup, journal, "s1", spec())
    sup.request_cancel("s1")
    tick_until(sup, lambda: journal.get("s1")["state"] == "KILLED")
    row = journal.get("s1")
    assert row["termination_verified"] == 0 and "NOT verified" in row["error"]
    assert sup.stuck_slots() == [0] and sup.free_slots() == 0
    journal.accept("next", spec(params={"iterations": 20_000}))
    sup.tick()
    assert journal.get("next")["state"] == "ACCEPTED"  # the stuck slot is not reused
    real_kill(proctree.tree(root) or procs)
    tick_until(sup, lambda: journal.get("next")["state"] != "ACCEPTED")
    assert sup.stuck_slots() == []


# ---------------------------------------------------------------- AC-4.5 spawn log

def test_spawn_log_one_per_job(journal, tmp_path):
    sup = Supervisor(journal, [[0], [1]], tmp_path / "jobs")
    for j in ("a", "b", "c"):
        assert journal.accept(j, spec(params={"iterations": 20_000}))
        assert not journal.accept(j, spec(params={"iterations": 20_000}))  # duplicate delivery
    tick_until(sup, lambda: all(journal.get(j)["state"] == "SUCCEEDED" for j in "abc"))
    assert journal.spawn_counts() == {"a": 1, "b": 1, "c": 1}


# ---------------------------------------------------------------- AC-4.6 restart recovery

def test_recovery_kills_journaled_orphan_and_fails_job(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    root, procs = start(sup, journal, "r1", spec("mem_touch", {"mem_mb": 16, "hold_s": 30}, mem_mb=64))
    journal.accept("r2", spec(params={"iterations": 20_000}))  # accepted, never spawned
    del sup                                                     # the agent "crashes"
    report = recover(journal, tmp_path / "jobs")
    assert report.killed == {"r1": True} and gone(procs)
    r1, r2 = journal.get("r1"), journal.get("r2")
    assert r1["state"] == "FAILED" and r1["fail_reason"] == "agent_restart" and r1["termination_verified"] == 1
    assert r2["state"] == "FAILED" and r2["fail_reason"] == "agent_restart" and r2["termination_verified"] is None


def _workload(job_dir, job_id):
    job_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([sys.executable, "-m", "worker.workloads", "mem_touch", "--job-id", job_id,
                             "--job-dir", str(job_dir), "--params", json.dumps({"mem_mb": 16, "hold_s": 30})],
                            cwd=str(sup_mod.REPO_ROOT))


def test_recovery_kills_unjournaled_process_only_in_own_jobs_dir(journal, tmp_path):
    mine = _workload((tmp_path / "jobs" / "ghost").resolve(), "ghost")
    other = _workload((tmp_path / "other-agent" / "jobs" / "x").resolve(), "x")
    try:
        time.sleep(1.0)
        mine_tree, other_tree = proctree.tree(psutil.Process(mine.pid)), proctree.tree(psutil.Process(other.pid))
        report = recover(journal, tmp_path / "jobs")
        assert report.killed == {"ghost": True} and gone(mine_tree)
        assert all(proctree.alive(p) for p in other_tree)
    finally:
        proctree.kill_verified(proctree.tree(psutil.Process(other.pid)))


def test_recovery_reports_real_outcome_when_result_exists(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    journal.accept("d1", spec(params={"iterations": 20_000}))
    sup.tick()
    row = journal.get("d1")
    assert row["state"] == "RUNNING"
    tick_until(sup, lambda: (tmp_path / "jobs" / "d1" / "result.json").exists(), 10)
    time.sleep(0.5)
    del sup  # crashed before reaping: journal still says RUNNING
    recover(journal, tmp_path / "jobs")
    row = journal.get("d1")
    assert row["state"] == "SUCCEEDED" and '"checksum"' in row["result_json"]


# ---------------------------------------------------------------- live stats (playground, D-028)

def test_job_stats_report_live_cpu_and_rss(journal, tmp_path):
    sup = Supervisor(journal, [[0]], tmp_path / "jobs")
    start(sup, journal, "ls1", spec())
    end = time.monotonic() + 3
    stats = {}
    while time.monotonic() < end:
        sup.tick()
        stats = sup.job_stats().get("ls1") or {}
        if (stats.get("cpu_pct") or 0) > 50:     # first samples cover the interpreter starting up
            break
        time.sleep(0.1)
    assert stats["rss_mb"] > 5 and stats["cpu_pct"] > 20    # a busy hash loop: most of one core
    sup.request_cancel("ls1")
    tick_until(sup, lambda: journal.get("ls1")["state"] == "KILLED")
    assert sup.job_stats() == {}
