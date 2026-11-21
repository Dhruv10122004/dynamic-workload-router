"""Controller API driven by a scripted fake worker (in-process TestClient)."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from controller.main import create_app
from controller.settings import Settings
from shared.schemas import SCHEMA_VERSION

ADMIN = {"X-Admin-Token": "adm"}
TOKEN = {"Authorization": "Bearer fog-secret"}


@pytest.fixture
def client(tmp_path):
    s = Settings(worker_tokens={"fog-1": "fog-secret", "edge-1": "edge-secret"}, data_dir=tmp_path,
                 admin_token="adm", scheduler_interval_s=0.05)
    with TestClient(create_app(s)) as c:
        yield c


def register(c, worker_id="fog-1", headers=TOKEN, **kw):
    body = {"schema_version": SCHEMA_VERSION, "worker_id": worker_id, "tier": "fog", "hostname": "linux-box",
            "os": "Linux", "os_version": "6.8", "python": "3.11.9", "cpu_logical": 4, "mem_total_mb": 8000,
            "slots": [{"slot": 0, "cpus": [1]}], "mem_budget_mb": 1536, "mem_floor_mb": 512,
            "supported_types": ["cpu_hash"], "capabilities": {}, "agent_version": "t",
            "agent_instance_id": "inst-1", **kw}
    return c.post("/api/v1/workers/register", json=body, headers=headers)


class FakeWorker:
    def __init__(self, c, instance="inst-1"):
        self.c, self.instance, self.seq = c, instance, 0

    def beat(self, jobs=(), seq=None):
        if seq is None:
            self.seq += 1
            seq = self.seq
        body = {"schema_version": SCHEMA_VERSION, "agent_instance_id": self.instance, "seq": seq,
                "telemetry": {"cpu_pct": 10, "own_cpu_pct": 0, "foreign_cpu_pct": 10, "mem_total_mb": 8000,
                              "mem_available_mb": 6000, "free_slots": 1},
                "jobs": list(jobs)}
        r = self.c.post("/api/v1/workers/fog-1/heartbeat", json=body, headers=TOKEN)
        assert r.status_code == 200, r.text
        return r.json()


def submit(c, **kw):
    body = {"type": "cpu_hash", "params": {"iterations": 20_000}, "placement": "fog-1", **kw}
    return c.post("/api/v1/jobs", json=body, headers=ADMIN)


def wait_state(c, job_id, state, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = c.get(f"/api/v1/jobs/{job_id}", headers=ADMIN).json()
        if job["state"] == state:
            return job
        time.sleep(0.02)
    raise AssertionError(f"{job_id} stuck in {job['state']}, wanted {state}")


def test_register_auth_and_schema(client):
    assert register(client, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert register(client, headers={}).status_code == 401
    assert register(client, worker_id="ghost").status_code == 401
    assert register(client, schema_version=999).status_code == 409
    r = register(client)
    assert r.status_code == 200 and r.json()["accepted"] is True
    w = client.get("/api/v1/workers/fog-1", headers=ADMIN).json()
    assert (w["hostname"], w["os"], w["fresh"]) == ("linux-box", "Linux", False)  # fresh only after a heartbeat
    FakeWorker(client).beat()
    assert client.get("/api/v1/workers/fog-1", headers=ADMIN).json()["fresh"] is True


def test_admin_endpoints_need_localhost_or_token(client):
    assert client.get("/api/v1/workers").status_code == 401  # TestClient's host is not 127.0.0.1
    assert client.get("/api/v1/workers", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/health").status_code == 200  # public


def test_submit_validation_422_creates_no_record(client):
    register(client)
    assert submit(client, params={"iterations": 5}).status_code == 422
    assert submit(client, type="nope").status_code == 422
    assert submit(client, placement="ghost").status_code == 422
    assert submit(client, timeout_s=500).status_code == 422
    assert client.get("/api/v1/jobs", headers=ADMIN).json() == []


def test_full_lifecycle_and_job_events(client):
    register(client)
    w = FakeWorker(client)
    w.beat()
    r = submit(client)
    assert r.status_code == 202 and r.json()["state"] == "PENDING"
    jid = r.json()["job_id"]
    wait_state(client, jid, "ASSIGNED")

    reply = w.beat()
    assert [a["job_id"] for a in reply["assignments"]] == [jid]
    assert reply["assignments"][0]["mem_mb"] == 48 and reply["assignments"][0]["max_runtime_s"] == 120
    # Level-triggered: re-delivered until the snapshot lists it.
    assert [a["job_id"] for a in w.beat()["assignments"]] == [jid]

    reply = w.beat([{"job_id": jid, "state": "RUNNING", "slot": 0, "pid": 4242, "pid_create_time": 1.0,
                     "spawn_s": 0.02}])
    assert reply["assignments"] == []
    job = wait_state(client, jid, "RUNNING")
    assert job["acked"] and job["pid"] == 4242 and job["start_ts"]

    result = {"exec_s": 0.5, "cpu_s": 0.49, "peak_rss_mb": 30.0, "checksum": "abc", "pid": 4243}
    done = {"job_id": jid, "state": "SUCCEEDED", "slot": 0, "pid": 4242, "exit_code": 0, "result": result}
    reply = w.beat([done])
    assert reply["ack_terminal"] == [jid]
    job = wait_state(client, jid, "SUCCEEDED")
    assert (job["exec_s"], job["cpu_s"], job["result"]["checksum"], job["hostname"]) == (0.5, 0.49, "abc", "linux-box")
    assert job["end_ts"] >= job["start_ts"] >= job["assign_ts"] >= job["submit_ts"]
    path = [(t["from_state"], t["to_state"]) for t in job["transitions"]]
    assert path == [(None, "PENDING"), ("PENDING", "ASSIGNED"), ("ASSIGNED", "RUNNING"), ("RUNNING", "SUCCEEDED")]
    assert job["decision"]["worker_id"] == "fog-1"

    # Ack lost -> worker repeats the terminal snapshot: still acked, no new event, no late result.
    assert w.beat([done])["ack_terminal"] == [jid]
    job = client.get(f"/api/v1/jobs/{jid}", headers=ADMIN).json()
    assert len(job["transitions"]) == 4 and job["late_result"] is None


def test_duplicate_seq_is_not_applied_but_gets_a_reply(client):
    register(client)
    w = FakeWorker(client)
    w.beat()
    jid = submit(client).json()["job_id"]
    wait_state(client, jid, "ASSIGNED")
    reply = w.beat([{"job_id": jid, "state": "RUNNING", "pid": 1}], seq=w.seq)  # same seq as before
    assert reply["seq_ack"] == w.seq
    assert client.get(f"/api/v1/jobs/{jid}", headers=ADMIN).json()["state"] == "ASSIGNED"


def test_worker_validation_failure_is_recorded(client):
    register(client)
    w = FakeWorker(client)
    w.beat()
    jid = submit(client).json()["job_id"]
    wait_state(client, jid, "ASSIGNED")
    w.beat([{"job_id": jid, "state": "FAILED", "fail_reason": "worker_validation", "error": "bad"}])
    job = wait_state(client, jid, "FAILED")
    assert job["fail_reason"] == "worker_validation" and job["transitions"][-1]["reason"] == "worker_validation"


def test_structural_reject_and_pending_when_stale(client):
    register(client, supported_types=["cpu_hash"])
    # Not fresh yet (no heartbeat): stays PENDING; the first attempt is recorded and flags overflow.
    jid = submit(client).json()["job_id"]
    time.sleep(0.3)
    job = client.get(f"/api/v1/jobs/{jid}", headers=ADMIN).json()
    assert job["state"] == "PENDING" and job["overflow"]
    assert job["decision"]["worker_id"] is None and job["decision"]["ineligible"] == {"fog-1": "stale"}
    assert not job["decision"]["structural_reject"]
    n = len(client.get("/api/v1/events", headers=ADMIN).json())
    time.sleep(0.2)  # later no-op passes add no decision rows
    assert client.get(f"/api/v1/jobs/{jid}", headers=ADMIN).json()["decision"]["id"] == job["decision"]["id"]
    assert len(client.get("/api/v1/events", headers=ADMIN).json()) == n
    # mem_touch is not supported by fog-1 -> REJECTED with the reason.
    jid2 = submit(client, type="mem_touch", params={"mem_mb": 64, "hold_s": 1}).json()["job_id"]
    job2 = wait_state(client, jid2, "REJECTED")
    assert job2["decision"]["structural_reject"] and job2["decision"]["ineligible"] == {"fog-1": "unsupported_type"}


def test_one_slot_is_never_overbooked(client):
    register(client)
    w = FakeWorker(client)
    w.beat()
    ids = [submit(client).json()["job_id"] for _ in range(3)]
    wait_state(client, ids[0], "ASSIGNED")
    time.sleep(0.3)
    states = [client.get(f"/api/v1/jobs/{j}", headers=ADMIN).json()["state"] for j in ids]
    assert states == ["ASSIGNED", "PENDING", "PENDING"]


def test_agent_restart_event(client):
    register(client)
    FakeWorker(client, "inst-1").beat()
    FakeWorker(client, "inst-2").beat()
    s = client.app.state.service
    kinds = [r["kind"] for r in s.db.all("SELECT kind FROM events ORDER BY id")]
    assert "agent_restarted" in kinds


def test_unknown_job_in_snapshot_is_acked(client):
    register(client)
    ghost = uuid.uuid4().hex
    reply = FakeWorker(client).beat([{"job_id": ghost, "state": "SUCCEEDED"}])
    assert reply["ack_terminal"] == [ghost]
