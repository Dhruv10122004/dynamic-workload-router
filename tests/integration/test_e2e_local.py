"""End-to-end on one machine: real controller process + real agent process + real job subprocess.

This is the local stand-in for M1's cross-machine ACs; the Windows->Linux run is
done by hand (docs/evidence/m1.md).
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

from tests.unit.test_workload_cpu_hash import _expected_checksum

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
ADMIN = {"X-Admin-Token": "adm"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_tree(p: subprocess.Popen) -> None:
    try:
        procs = [psutil.Process(p.pid), *psutil.Process(p.pid).children(recursive=True)]
    except psutil.NoSuchProcess:
        return
    for proc in procs:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=10)


@pytest.fixture
def stack(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("DWR_")}
    env.update(DWR_PORT=str(port), DWR_BIND_HOSTS="127.0.0.1", DWR_CONTROLLER_DATA_DIR=str(tmp_path / "ctl"),
               DWR_WORKER_TOKENS="edge-1:edge-secret", DWR_ADMIN_TOKEN="adm",
               DWR_WORKER_ID="edge-1", DWR_TIER="edge", DWR_CONTROLLER_URL=f"http://127.0.0.1:{port}",
               DWR_WORKER_TOKEN="edge-secret", DWR_SLOT_CPUS="0", DWR_MEM_BUDGET_MB="1024",
               DWR_MEM_FLOOR_MB="64", DWR_DATA_DIR=str(tmp_path / "wrk"))
    logs = {n: open(tmp_path / f"{n}.log", "wb") for n in ("controller", "agent")}
    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, "-m", "controller", "--env-file", "-"], cwd=REPO, env=env,
                                      stdout=logs["controller"], stderr=subprocess.STDOUT))
        base = f"http://127.0.0.1:{port}/api/v1"
        with httpx.Client(base_url=base, headers=ADMIN, timeout=5) as c:
            _wait(lambda: _ok(c, "/health"), 20, "controller did not start")
            procs.append(subprocess.Popen([sys.executable, "-m", "worker.agent", "--env-file", "-"], cwd=REPO,
                                          env=env, stdout=logs["agent"], stderr=subprocess.STDOUT))
            _wait(lambda: c.get("/health").json()["status"] == "READY", 20, "worker never became fresh")
            yield c, tmp_path
    finally:
        for p in reversed(procs):
            _kill_tree(p)
        for f in logs.values():
            f.close()


def _ok(c, path):
    try:
        return c.get(path).status_code == 200
    except httpx.HTTPError:
        return False


def _wait(pred, timeout, msg):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError(msg)


def _wait_terminal(c, job_id, timeout=30):
    _wait(lambda: c.get(f"/jobs/{job_id}").json()["state"] not in
          ("PENDING", "ASSIGNED", "ACCEPTED", "RUNNING"), timeout, f"job {job_id} did not finish")
    return c.get(f"/jobs/{job_id}").json()


def test_cpu_hash_end_to_end(stack):
    c, tmp = stack
    w = c.get("/workers/edge-1").json()
    assert w["hostname"] == socket.gethostname() and w["fresh"] and w["supported_types"] == ["cpu_hash"]

    assert c.post("/jobs", json={"type": "cpu_hash", "params": {"iterations": 5}, "placement": "edge-1"}).status_code == 422

    r = c.post("/jobs", json={"type": "cpu_hash", "params": {"iterations": 300_000}, "placement": "edge-1"})
    assert r.status_code == 202
    job = _wait_terminal(c, r.json()["job_id"])
    assert job["state"] == "SUCCEEDED", job
    assert job["hostname"] == socket.gethostname() and job["pid"] > 0
    assert job["exec_s"] > 0 and job["cpu_s"] > 0 and job["spawn_s"] is not None
    assert job["result"]["checksum"] == _expected_checksum(300_000)
    states = [t["to_state"] for t in job["transitions"]]
    assert states[0] == "PENDING" and states[1] == "ASSIGNED" and states[-1] == "SUCCEEDED"
    assert all(t["trigger"] for t in job["transitions"])

    # The worker journal ran the job exactly once and got the terminal ack.
    _wait(lambda: _journal(tmp, job["job_id"])["reported_terminal"] == 1, 10, "terminal never acked")
    row = _journal(tmp, job["job_id"])
    assert (row["state"], row["pid"]) == ("SUCCEEDED", job["pid"])


def test_worker_rejects_invalid_params_that_bypassed_the_controller(stack):
    c, tmp = stack
    job_id = "bypass-" + str(time.time_ns())
    now = time.time()
    with sqlite3.connect(tmp / "ctl" / "dwr.db", timeout=10) as db:
        db.execute(
            """INSERT INTO jobs (job_id, type, params_json, mem_mb, max_runtime_s, placement, state, worker_id,
                 submit_ts, assign_ts) VALUES (?, 'cpu_hash', ?, 40, 120, 'edge-1', 'ASSIGNED', 'edge-1', ?, ?)""",
            (job_id, json.dumps({"iterations": 5}), now, now))
        db.execute("INSERT INTO job_events (job_id, ts, from_state, to_state, trigger) VALUES (?, ?, NULL, 'ASSIGNED', 'test_bypass')",
                   (job_id, now))
    job = _wait_terminal(c, job_id)
    assert (job["state"], job["fail_reason"]) == ("FAILED", "worker_validation"), job
    assert job["pid"] is None  # never spawned
    assert job["transitions"][-1]["to_state"] == "FAILED"


def _journal(tmp, job_id):
    with sqlite3.connect(tmp / "wrk" / "journal.db", timeout=10) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
