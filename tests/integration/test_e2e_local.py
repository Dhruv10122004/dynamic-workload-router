"""End-to-end on one machine: real controller process + two real agent processes
(edge-1 and a simulated fog-1) + real job subprocesses.

This is the local stand-in for the cross-machine ACs of M1/M2; the Windows->Linux run
is done by hand (docs/evidence/m1.md, m2.md).
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

from controller.blobs import generate
from tests.unit.test_workload_cpu_hash import _expected_checksum

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]
ADMIN = {"X-Admin-Token": "adm"}
NCPU = psutil.cpu_count() or 1
# Two slots per worker on distinct cores when the host has them.
SLOTS = {"edge-1": "1;2", "fog-1": "3;4"} if NCPU >= 5 else {"edge-1": "0", "fog-1": "0"}


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


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("DWR_")}
    ctl_env = {**base_env, "DWR_PORT": str(port), "DWR_BIND_HOSTS": "127.0.0.1",
               "DWR_CONTROLLER_DATA_DIR": str(tmp / "ctl"), "DWR_ADMIN_TOKEN": "adm",
               "DWR_WORKER_TOKENS": "edge-1:edge-secret,fog-1:fog-secret", "DWR_CONTROLLER_CPUS": "0"}
    logs = {n: open(tmp / f"{n}.log", "wb") for n in ("controller", "edge-1", "fog-1")}
    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, "-m", "controller", "--env-file", "-"], cwd=REPO,
                                      env=ctl_env, stdout=logs["controller"], stderr=subprocess.STDOUT))
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers=ADMIN, timeout=10) as c:
            _wait(lambda: _ok(c, "/health"), 20, "controller did not start")
            for wid, tier in (("edge-1", "edge"), ("fog-1", "fog")):
                env = {**base_env, "DWR_WORKER_ID": wid, "DWR_TIER": tier,
                       "DWR_CONTROLLER_URL": f"http://127.0.0.1:{port}", "DWR_WORKER_TOKEN": wid.split("-")[0] + "-secret",
                       "DWR_SLOT_CPUS": SLOTS[wid], "DWR_MEM_BUDGET_MB": "1024", "DWR_MEM_FLOOR_MB": "64",
                       "DWR_DATA_DIR": str(tmp / wid)}
                procs.append(subprocess.Popen([sys.executable, "-m", "worker.agent", "--env-file", "-"], cwd=REPO,
                                              env=env, stdout=logs[wid], stderr=subprocess.STDOUT))
            _wait(lambda: len(c.get("/health").json()["fresh_workers"]) == 2, 20, "workers never became fresh")
            yield c, tmp
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


def _submit(c, worker, type_, params):
    r = c.post("/jobs", json={"type": type_, "params": params, "placement": worker})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def _wait_terminal(c, job_id, timeout=60):
    _wait(lambda: c.get(f"/jobs/{job_id}").json()["state"] not in
          ("PENDING", "ASSIGNED", "ACCEPTED", "RUNNING"), timeout, f"job {job_id} did not finish")
    return c.get(f"/jobs/{job_id}").json()


def _journal(tmp, worker, job_id):
    with sqlite3.connect(tmp / worker / "journal.db", timeout=10) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


# ---------------------------------------------------------------- M1

def test_cpu_hash_end_to_end(stack):
    c, tmp = stack
    w = c.get("/workers/edge-1").json()
    assert w["hostname"] == socket.gethostname() and w["fresh"]
    assert w["supported_types"] == ["cpu_hash", "data_digest", "mem_touch"]
    assert c.get("/health").json()["controller_cpus"] == [0]  # DWR_CONTROLLER_CPUS applied

    assert c.post("/jobs", json={"type": "cpu_hash", "params": {"iterations": 5}, "placement": "edge-1"}).status_code == 422

    job = _wait_terminal(c, _submit(c, "edge-1", "cpu_hash", {"iterations": 300_000}))
    assert job["state"] == "SUCCEEDED", job
    assert job["hostname"] == socket.gethostname() and job["pid"] > 0
    assert job["exec_s"] > 0 and job["cpu_s"] > 0 and job["spawn_s"] is not None
    assert job["result"]["checksum"] == _expected_checksum(300_000)
    states = [t["to_state"] for t in job["transitions"]]
    assert states[0] == "PENDING" and states[1] == "ASSIGNED" and states[-1] == "SUCCEEDED"

    # The worker journal ran the job exactly once and got the terminal ack.
    _wait(lambda: _journal(tmp, "edge-1", job["job_id"])["reported_terminal"] == 1, 10, "terminal never acked")
    assert _journal(tmp, "edge-1", job["job_id"])["pid"] == job["pid"]


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


# ---------------------------------------------------------------- M2

@pytest.mark.parametrize("worker", ["edge-1", "fog-1"])
def test_all_three_types_on_each_worker(stack, worker):
    c, _ = stack
    size = 3 * 1024 * 1024 + 17
    ids = {
        "cpu_hash": _submit(c, worker, "cpu_hash", {"iterations": 50_000}),
        "data_digest": _submit(c, worker, "data_digest", {"input_bytes": size, "rounds": 2}),
        "mem_touch": _submit(c, worker, "mem_touch", {"mem_mb": 64, "hold_s": 1}),
    }
    jobs = {t: _wait_terminal(c, j) for t, j in ids.items()}
    for t, job in jobs.items():
        assert job["state"] == "SUCCEEDED", (t, job)
        assert job["worker_id"] == worker and job["pid"] > 0

    dd = jobs["data_digest"]
    assert dd["transfer_bytes"] == dd["input_bytes"] == size and dd["transfer_s"] > 0  # AC-2.3 (over localhost)
    data = generate(size)
    import hashlib
    digest = b""
    for _ in range(2):
        digest = hashlib.sha256(digest + data).digest()
    assert dd["result"]["checksum"] == digest.hex() and dd["result"]["bytes_hashed"] == 2 * size

    mt = jobs["mem_touch"]["result"]
    assert jobs["mem_touch"]["exec_s"] >= 1.0 and abs(mt["hold_s_actual"] - 1.0) < 0.3  # AC-2.4 (scaled)
    assert mt["exec_s"] == pytest.approx(mt["touch_s"] + mt["hold_s_actual"])
    assert mt["peak_rss_mb"] >= 64


def test_two_slots_run_concurrently_on_their_own_cores(stack):
    c, _ = stack
    if NCPU < 5:
        pytest.skip("needs >= 5 logical cores")
    ids = [_submit(c, "edge-1", "mem_touch", {"mem_mb": 16, "hold_s": 2}) for _ in range(2)]
    _wait(lambda: [c.get(f"/jobs/{j}").json()["state"] for j in ids].count("RUNNING") == 2, 15,
          "both slots never ran at the same time")
    w = c.get("/workers/edge-1").json()
    assert w["slots_used"] == 2 and w["slots_total"] == 2
    jobs = [_wait_terminal(c, j) for j in ids]
    assert sorted(tuple(j["result"]["cpus_affinity"]) for j in jobs) == [(1,), (2,)]


def test_input_download_requires_the_assigned_workers_token(stack):
    c, _ = stack
    job_id = _submit(c, "fog-1", "data_digest", {"input_bytes": 1024, "rounds": 1})
    base = str(c.base_url).rstrip("/")
    _wait_terminal(c, job_id)
    url = f"{base}/jobs/{job_id}/input"
    for token in ("edge-secret", "nope"):  # another worker's token, unknown token: 401, never 409
        r = httpx.get(url, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, (token, r.status_code)
    r = httpx.get(url, headers={"Authorization": "Bearer fog-secret"})
    assert r.status_code == 409  # terminal: input no longer served


def test_worker_freshness_updates_every_second(stack):
    c, _ = stack
    ages = []
    for _ in range(4):
        ages.append(c.get("/workers/fog-1").json()["heartbeat_age_s"])
        time.sleep(0.5)
    assert max(ages) < 2.0
