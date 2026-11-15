"""M4 end to end on this machine: real controller + real agent + real job processes.
Cancel, timeout, RSS watchdog, agent crash + restart recovery, spawn uniqueness
(AC-4.1, 4.2, 4.3, 4.5, 4.6 local stand-in; the Linux side is run by hand, docs/evidence/m4.md)."""
import os
import subprocess
import sys
import time

import httpx
import psutil
import pytest

from tests.integration.test_e2e_local import ADMIN, NCPU, REPO, _free_port, _kill_tree, _ok, _wait
from worker import proctree
from worker.journal import Journal

pytestmark = pytest.mark.integration
SLOTS = "1;2" if NCPU >= 3 else "0"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e_m4")
    port = _free_port()
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("DWR_")}
    ctl_env = {**base_env, "DWR_PORT": str(port), "DWR_BIND_HOSTS": "127.0.0.1", "DWR_TEST_MODE": "1",
               "DWR_CONTROLLER_DATA_DIR": str(tmp / "ctl"), "DWR_ADMIN_TOKEN": "adm",
               "DWR_WORKER_TOKENS": "edge-1:edge-secret", "DWR_CONTROLLER_CPUS": "0"}
    agent_env = {**base_env, "DWR_WORKER_ID": "edge-1", "DWR_TIER": "edge",
                 "DWR_CONTROLLER_URL": f"http://127.0.0.1:{port}", "DWR_WORKER_TOKEN": "edge-secret",
                 "DWR_SLOT_CPUS": SLOTS, "DWR_MEM_BUDGET_MB": "1024", "DWR_MEM_FLOOR_MB": "64",
                 "DWR_DATA_DIR": str(tmp / "edge-1")}
    log = open(tmp / "all.log", "ab")
    state = {"procs": []}

    def start_agent():
        p = subprocess.Popen([sys.executable, "-m", "worker.agent", "--env-file", "-"], cwd=REPO,
                             env=agent_env, stdout=log, stderr=subprocess.STDOUT)
        state["agent"] = p
        state["procs"].append(p)
        return p

    try:
        state["procs"].append(subprocess.Popen([sys.executable, "-m", "controller", "--env-file", "-"], cwd=REPO,
                                               env=ctl_env, stdout=log, stderr=subprocess.STDOUT))
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers=ADMIN, timeout=10) as c:
            _wait(lambda: _ok(c, "/health"), 20, "controller did not start")
            start_agent()
            _wait(lambda: c.get("/health").json()["fresh_workers"] == ["edge-1"], 20, "agent never fresh")
            yield c, tmp, start_agent, state
    finally:
        for p in reversed(state["procs"]):
            _kill_tree(p)
        log.close()


def submit(c, type_, params, **kw):
    r = c.post("/jobs", json={"type": type_, "params": params, "placement": "edge-1", **kw})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def running_tree(c, jid):
    _wait(lambda: c.get(f"/jobs/{jid}").json()["state"] == "RUNNING", 20, "job never RUNNING")
    j = c.get(f"/jobs/{jid}").json()
    root = psutil.Process(j["pid"])
    assert abs(root.create_time() - j["pid_create_time"]) < 0.01
    time.sleep(0.5)  # launcher -> interpreter
    return proctree.tree(root)


def final(c, jid, timeout=30):
    _wait(lambda: c.get(f"/jobs/{jid}").json()["state"] not in ("PENDING", "ASSIGNED", "ACCEPTED", "RUNNING"),
          timeout, f"{jid} did not end")
    return c.get(f"/jobs/{jid}").json()


def gone(procs):
    return not any(proctree.alive(p) for p in procs)


def test_cancel_running_job_is_killed_and_verified(stack):
    c, *_ = stack
    jid = submit(c, "cpu_hash", {"iterations": 5_000_000})
    procs = running_tree(c, jid)
    r = c.post(f"/jobs/{jid}/cancel")
    assert r.status_code == 202 and r.json()["cancel_requested"] is True
    j = final(c, jid)
    assert j["state"] == "KILLED" and j["kill_reason"] == "cancel" and j["termination_verified"] is True
    assert j["kill_latency_ms"] > 0 and gone(procs)


def test_timeout_kills(stack):
    c, *_ = stack
    jid = submit(c, "cpu_hash", {"iterations": 5_000_000}, timeout_s=1.0)
    j = final(c, jid)
    assert j["state"] == "KILLED" and j["kill_reason"] == "timeout" and j["termination_verified"] is True


def test_memory_overallocation_is_killed(stack):
    c, *_ = stack
    jid = submit(c, "mem_touch", {"mem_mb": 16, "hold_s": 20, "actual_mb_override": 400})
    j = final(c, jid)
    assert j["state"] == "KILLED" and j["kill_reason"] == "memory_limit" and j["termination_verified"] is True


def test_agent_crash_and_restart_kills_orphan(stack):
    c, tmp, start_agent, state = stack
    jid = submit(c, "mem_touch", {"mem_mb": 16, "hold_s": 30})
    job_procs = running_tree(c, jid)
    job_pids = {p.pid for p in job_procs}
    agent_only = [p for p in proctree.tree(psutil.Process(state["agent"].pid)) if p.pid not in job_pids]
    proctree.kill_verified(agent_only)                       # the agent "crashes"; its job lives on
    assert not gone(job_procs), "the job should survive the agent as an orphan"
    start_agent()
    j = final(c, jid)
    assert j["state"] == "FAILED" and j["fail_reason"] == "agent_restart" and j["termination_verified"] is True
    assert gone(job_procs)
    kinds = [e["kind"] for e in c.get("/events").json()]
    assert "agent_restarted" in kinds
    # still usable afterwards
    ok = submit(c, "cpu_hash", {"iterations": 20_000})
    assert final(c, ok)["state"] == "SUCCEEDED"


def test_no_job_was_ever_spawned_twice(stack):
    _, tmp, *_ = stack
    j = Journal(tmp / "edge-1" / "journal.db")
    try:
        counts = j.spawn_counts()
    finally:
        j.close()
    assert counts and all(n == 1 for n in counts.values()), counts
