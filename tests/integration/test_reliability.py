"""M4 controller reliability with scripted fake workers: cancel, staleness -> UNKNOWN ->
reconciliation, lost and queue timeouts, drain, controller restart (AC-4.1, 4.4, 4.5, 4.7)."""
import time

import pytest
from fastapi.testclient import TestClient

from controller.main import create_app
from controller.settings import Settings
from tests.integration.test_scheduling import ADMIN, TOKENS, Worker, events, job, settle, submit

FAST = dict(stale_s=0.5, maintenance_interval_s=0.1, scheduler_interval_s=0.05)


def app(tmp_path, **kw):
    return create_app(Settings(worker_tokens=TOKENS, data_dir=tmp_path, admin_token="adm", **{**FAST, **kw}))


@pytest.fixture
def client(tmp_path):
    with TestClient(app(tmp_path)) as c:
        yield c


def cancel(c, jid):
    return c.post(f"/api/v1/jobs/{jid}/cancel", headers=ADMIN)


def wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.03)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------- cancel

def test_cancel_pending_assigned_running_and_terminal(client):
    assert cancel(client, "nope").status_code == 404
    jid = submit(client)                                    # no worker yet: PENDING
    r = cancel(client, jid)
    assert r.status_code == 202 and r.json()["state"] == "CANCELLED"
    assert cancel(client, jid).status_code == 409           # terminal
    fog = Worker(client, "fog-1", "fog", slots=2)
    fog.beat()
    a, b = submit(client), submit(client)
    wait(lambda: job(client, a)["state"] == "ASSIGNED" and job(client, b)["state"] == "ASSIGNED")
    reply = fog.beat()                                      # both delivered, now RUNNING on the worker
    assert {x["job_id"] for x in reply["assignments"]} == {a, b}
    fog.beat()
    assert job(client, a)["state"] == "RUNNING"
    assert cancel(client, a).json() == {"job_id": a, "cancel_requested": True, "state": "RUNNING"}
    # level-triggered: the cancel is repeated until the worker reports an outcome
    assert fog.beat()["cancel"] == [a] and fog.beat()["cancel"] == [a]
    del fog.running[a]
    fog.c.post(f"/api/v1/workers/fog-1/heartbeat", headers=fog.headers, json={
        "schema_version": 1, "agent_instance_id": "fog-1-inst", "seq": fog.seq + 1,
        "telemetry": {"cpu_pct": 5, "own_cpu_pct": 0, "foreign_cpu_pct": 5, "mem_total_mb": 16000,
                      "mem_available_mb": 12000, "free_slots": 1},
        "jobs": [{"job_id": a, "state": "KILLED", "kill_reason": "cancel", "termination_verified": True,
                  "kill_latency_ms": 120.0}, {"job_id": b, "state": "RUNNING"}]})
    fog.seq += 1
    ja = job(client, a)
    assert ja["state"] == "KILLED" and ja["kill_reason"] == "cancel" and ja["termination_verified"]
    assert ja["kill_latency_ms"] == 120.0
    assert fog.beat()["cancel"] == []


def test_cancel_assigned_job_reaches_worker_with_its_assignment(client):
    fog = Worker(client, "fog-1", "fog")
    fog.beat()
    jid = submit(client)
    wait(lambda: job(client, jid)["state"] == "ASSIGNED")
    cancel(client, jid)
    reply = fog.beat()
    assert [x["job_id"] for x in reply["assignments"]] == [jid] and reply["cancel"] == [jid]


# ---------------------------------------------------------------- AC-4.4 staleness and reconciliation

def test_stale_worker_jobs_go_unknown_new_jobs_avoid_it_then_reconcile(client):
    edge, fog = Worker(client, "edge-1", "edge"), Worker(client, "fog-1", "fog")
    edge.beat(), fog.beat()
    first = submit(client)                                  # Edge
    second = submit(client)                                 # Fog
    settle(client, [edge, fog], lambda: job(client, second)["state"] == "RUNNING")
    t_cut = time.monotonic()                                # Fog's network is "unplugged"; Edge keeps beating
    settle(client, [edge], lambda: job(client, second)["state"] == "UNKNOWN", 3)
    assert time.monotonic() - t_cut < 0.5 + 1.0             # STALE_S + one sweep
    assert [e["worker_id"] for e in events(client, "worker_stale")] == ["fog-1"]
    assert client.get("/api/v1/workers/fog-1", headers=ADMIN).json()["stale"] is True
    edge.finish(first)
    third = submit(client)
    settle(client, [edge], lambda: job(client, third)["state"] == "RUNNING")
    assert job(client, third)["worker_id"] == "edge-1"     # new work avoided the stale worker
    # Fog comes back; the job had finished meanwhile.
    fog.finish(second)
    fog.beat()
    j = job(client, second)
    assert j["state"] == "SUCCEEDED"
    assert [t["to_state"] for t in j["transitions"]][-2:] == ["UNKNOWN", "SUCCEEDED"]
    assert events(client, "worker_fresh")[0]["worker_id"] == "fog-1"


def test_reconcile_unacked_is_redelivered_acked_is_lost(client):
    fog = Worker(client, "fog-1", "fog", slots=2)
    fog.beat()
    a = submit(client)
    wait(lambda: job(client, a)["state"] == "ASSIGNED")
    fog.beat()                                              # a delivered, worker journals it
    fog.beat()                                              # a listed in the snapshot -> acked
    assert job(client, a)["acked"]
    b = submit(client)
    wait(lambda: job(client, b)["state"] == "ASSIGNED")     # b assigned but never delivered
    wait(lambda: job(client, a)["state"] == "UNKNOWN" and job(client, b)["state"] == "UNKNOWN", 3)
    fog.running.clear()                                     # the worker lost its journal
    reply = fog.beat()
    ja, jb = job(client, a), job(client, b)
    assert ja["state"] == "FAILED" and ja["fail_reason"] == "lost"
    assert jb["state"] == "ASSIGNED" and [x["job_id"] for x in reply["assignments"]] == [b]  # same worker


def test_lost_timeout(tmp_path):
    with TestClient(app(tmp_path, lost_timeout_s=0.5)) as c:
        fog = Worker(c, "fog-1", "fog")
        fog.beat()
        jid = submit(c)
        settle(c, [fog], lambda: job(c, jid)["state"] == "RUNNING")
        wait(lambda: job(c, jid)["state"] == "FAILED", 4)
        j = job(c, jid)
        assert j["fail_reason"] == "lost" and [t["to_state"] for t in j["transitions"]][-2:] == ["UNKNOWN", "FAILED"]
        fog.finish(jid)                                     # the result arrives too late
        fog.beat()
        j = job(c, jid)
        assert j["state"] == "FAILED" and j["late_result"]["state"] == "SUCCEEDED"


def test_queue_timeout(client):
    assert client.put("/api/v1/scheduler", json={"queue_timeout_s": 0.3}, headers=ADMIN).status_code == 200
    jid = submit(client)                                    # no worker at all
    wait(lambda: job(client, jid)["state"] == "FAILED", 3)
    j = job(client, jid)
    assert j["fail_reason"] == "queue_timeout" and j["end_ts"] is not None


def test_drain_and_enable(client):
    fog = Worker(client, "fog-1", "fog")
    fog.beat()
    assert client.post("/api/v1/workers/fog-1/drain", headers=ADMIN).json()["draining"] is True
    jid = submit(client)
    time.sleep(0.3)
    fog.beat()
    j = job(client, jid)
    assert j["state"] == "PENDING" and j["decision"]["ineligible"] == {"fog-1": "draining"}
    client.post("/api/v1/workers/fog-1/enable", headers=ADMIN)
    settle(client, [fog], lambda: job(client, jid)["state"] == "RUNNING")
    assert client.post("/api/v1/workers/ghost/drain", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------- AC-4.7 controller restart

def test_controller_restart_keeps_everything_and_reconciles(tmp_path):
    with TestClient(app(tmp_path)) as c:
        fog = Worker(c, "fog-1", "fog", slots=2)
        fog.beat()
        done, running = submit(c), submit(c)
        settle(c, [fog], lambda: job(c, running)["state"] == "RUNNING" and job(c, done)["state"] == "RUNNING")
        fog.finish(done)
        settle(c, [fog], lambda: job(c, done)["state"] == "SUCCEEDED")
        before = {j: job(c, j) for j in (done, running)}
        n_events = len(c.get("/api/v1/events", headers=ADMIN).json())
    # controller is down; the worker keeps running and finishes the job meanwhile
    with TestClient(app(tmp_path)) as c:
        fog.c = c
        for j in (done, running):
            after = job(c, j)
            assert after["transitions"] == before[j]["transitions"] and after["decision"] == before[j]["decision"]
        assert len(c.get("/api/v1/events", headers=ADMIN).json()) == n_events
        fog.finish(running)
        reply = fog.beat()                                  # first heartbeat after the restart: accepted
        assert reply["assignments"] == []                   # nothing redelivered
        j = job(c, running)
        assert j["state"] == "SUCCEEDED"
        decisions = [d for d in (job(c, x)["decision"] for x in (done, running))]
        assert all(d["worker_id"] == "fog-1" for d in decisions)
        rows = c.get("/api/v1/jobs", headers=ADMIN).json()
        assert len(rows) == 2                                # no duplicate jobs


def test_controller_restart_unheard_worker_goes_stale(tmp_path):
    with TestClient(app(tmp_path)) as c:
        fog = Worker(c, "fog-1", "fog")
        fog.beat()
        jid = submit(c)
        settle(c, [fog], lambda: job(c, jid)["state"] == "RUNNING")
    with TestClient(app(tmp_path)) as c:
        assert c.get("/api/v1/workers/fog-1", headers=ADMIN).json()["fresh"] is False
        wait(lambda: job(c, jid)["state"] == "UNKNOWN", 3)  # not heard within STALE_S of startup
        fog.c = c
        fog.beat()                                          # comes back, still running it
        assert job(c, jid)["state"] == "RUNNING"
