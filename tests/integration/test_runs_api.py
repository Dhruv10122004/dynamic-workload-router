"""M6 runs API (plan §8.2, §12.7): create / finish / list / export, with fake workers."""
import csv
import io
import time

import pytest
from fastapi.testclient import TestClient

from controller.main import create_app
from controller.settings import Settings
from tests.integration.test_scheduling import ADMIN, TOKENS, Worker, job, settle, submit


@pytest.fixture
def client(tmp_path):
    s = Settings(worker_tokens=TOKENS, data_dir=tmp_path, admin_token="adm", scheduler_interval_s=0.05,
                 telemetry_sample_s=0.2, frontend_dist=None)
    with TestClient(create_app(s)) as c:
        yield c


def rows(c, run_id, table):
    r = c.get(f"/api/v1/runs/{run_id}/export", params={"table": table}, headers=ADMIN)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(r.text)))


def test_run_lifecycle_and_export(client):
    edge, fog = Worker(client, "edge-1", "edge"), Worker(client, "fog-1", "fog")
    edge.beat(), fog.beat()
    outside = submit(client)                                  # before the run, not part of it
    settle(client, [edge, fog], lambda: job(client, outside)["state"] == "SUCCEEDED", auto_finish=True)
    client.put("/api/v1/scheduler", json={"policy": "EdgeOnly"}, headers=ADMIN)
    r = client.post("/api/v1/runs", json={"name": "t1", "trace_hash": "abc", "git_commit": "deadbeef",
                                          "config": {"load": 1.5}}, headers=ADMIN)
    assert r.status_code == 201
    run = r.json()
    rid = run["run_id"]
    assert run["policy"] == "EdgeOnly" and run["config"]["load"] == 1.5 and run["config"]["scheduler"]["policy"] == "EdgeOnly"
    assert client.post("/api/v1/runs", json={"name": "t2"}, headers=ADMIN).status_code == 409   # one open run
    active = client.get("/api/v1/runs", params={"active": "true"}, headers=ADMIN).json()
    assert [x["run_id"] for x in active] == [rid] and active[0]["elapsed_s"] >= 0
    ids = [submit(client, run_id=rid, trace_index=i) for i in range(3)]
    settle(client, [edge, fog], lambda: all(job(client, j)["state"] == "SUCCEEDED" for j in ids), auto_finish=True)
    time.sleep(0.5)                                          # at least one telemetry sample in the window
    fin = client.post(f"/api/v1/runs/{rid}/finish", headers=ADMIN).json()
    assert fin["end_ts"] is not None
    jobs = rows(client, rid, "jobs")
    assert [int(j["trace_index"]) for j in jobs] == [0, 1, 2] and {j["job_id"] for j in jobs} == set(ids)
    assert all(j["tier"] == "edge" and j["state"] == "SUCCEEDED" and float(j["completion_s"]) >= 0 for j in jobs)
    dec = rows(client, rid, "decisions")
    assert {d["job_id"] for d in dec} == set(ids) and all(d["policy"] == "EdgeOnly" for d in dec)
    assert rows(client, rid, "telemetry")
    kinds = {e["kind"] for e in rows(client, rid, "events")}
    assert "run_started" in kinds and "run_finished" in kinds
    assert client.get(f"/api/v1/runs/{rid}/export", params={"table": "nope"}, headers=ADMIN).status_code == 422
    assert client.get("/api/v1/runs/ghost/export", params={"table": "jobs"}, headers=ADMIN).status_code == 404
    # finished runs accept no more jobs
    r = client.post("/api/v1/jobs", json={"type": "cpu_hash", "params": {"iterations": 20000}, "run_id": rid},
                    headers=ADMIN)
    assert r.status_code == 422


def test_run_refused_while_jobs_are_in_flight(client):
    submit(client)                                            # no worker: stays PENDING
    r = client.post("/api/v1/runs", json={"name": "busy"}, headers=ADMIN)
    assert r.status_code == 409 and "not terminal" in r.json()["detail"]


def test_unknown_run_id_on_submit_is_rejected(client):
    r = client.post("/api/v1/jobs", json={"type": "cpu_hash", "params": {"iterations": 20000}, "run_id": "nope"},
                    headers=ADMIN)
    assert r.status_code == 422
