"""M3 scheduling through the real controller with scripted fake workers (AC-3.2 .. AC-3.5)."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from controller.main import create_app
from controller.settings import Settings
from experiments.audit import audit
from shared.schemas import SCHEMA_VERSION

ADMIN = {"X-Admin-Token": "adm"}
TOKENS = {"edge-1": "edge-secret", "fog-1": "fog-secret"}
TERMINAL = {"SUCCEEDED", "FAILED", "KILLED", "CANCELLED", "REJECTED"}


def make_settings(tmp_path, **kw):
    return Settings(worker_tokens=TOKENS, data_dir=tmp_path, admin_token="adm", scheduler_interval_s=0.05, **kw)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


class Worker:
    """Registers, heartbeats, and runs assigned jobs on demand (`finish`) or immediately (`auto`)."""

    def __init__(self, c, wid, tier, slots=1, budget=1536, types=("cpu_hash", "mem_touch")):
        self.c, self.wid, self.seq = c, wid, 0
        self.headers = {"Authorization": f"Bearer {TOKENS[wid]}"}
        self.foreign = 5.0
        self.running: dict[str, str] = {}   # job_id -> state reported
        self.done: list[str] = []           # terminal, waiting for ack
        body = {"schema_version": SCHEMA_VERSION, "worker_id": wid, "tier": tier, "hostname": f"{wid}-host",
                "os": "Linux", "os_version": "6", "python": "3.11", "cpu_logical": 8, "mem_total_mb": 16000,
                "slots": [{"slot": i, "cpus": [i + 1]} for i in range(slots)], "mem_budget_mb": budget,
                "mem_floor_mb": 512, "supported_types": list(types), "capabilities": {}, "agent_version": "t",
                "agent_instance_id": f"{wid}-inst"}
        assert c.post("/api/v1/workers/register", json=body, headers=self.headers).status_code == 200

    def beat(self, auto_finish=False):
        self.seq += 1
        jobs = [{"job_id": j, "state": s} for j, s in self.running.items()]
        jobs += [{"job_id": j, "state": "SUCCEEDED", "result": {"exec_s": 0.01}} for j in self.done]
        body = {"schema_version": SCHEMA_VERSION, "agent_instance_id": f"{self.wid}-inst", "seq": self.seq,
                "telemetry": {"cpu_pct": self.foreign, "own_cpu_pct": 0, "foreign_cpu_pct": self.foreign,
                              "mem_total_mb": 16000, "mem_available_mb": 12000, "free_slots": 1},
                "jobs": jobs}
        r = self.c.post(f"/api/v1/workers/{self.wid}/heartbeat", json=body, headers=self.headers)
        assert r.status_code == 200, r.text
        reply = r.json()
        self.done = [j for j in self.done if j not in reply["ack_terminal"]]
        for a in reply["assignments"]:
            self.running[a["job_id"]] = "RUNNING"
        if auto_finish:
            self.finish_all()
        return reply

    def finish(self, job_id):
        del self.running[job_id]
        self.done.append(job_id)

    def finish_all(self):
        for j in list(self.running):
            self.finish(j)


def submit(c, mem=None, placement="auto", **kw):
    body = ({"type": "mem_touch", "params": {"mem_mb": mem, "hold_s": 0}} if mem
            else {"type": "cpu_hash", "params": {"iterations": 20_000}})
    r = c.post("/api/v1/jobs", json={**body, "placement": placement, **kw}, headers=ADMIN)
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def job(c, jid):
    return c.get(f"/api/v1/jobs/{jid}", headers=ADMIN).json()


def settle(c, workers, until, timeout=10.0, auto_finish=False):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in workers:
            w.beat(auto_finish=auto_finish)
        if until():
            return
        time.sleep(0.03)
    raise AssertionError("condition not reached")


def events(c, kind):
    return [e for e in c.get("/api/v1/events", params={"kind": kind}, headers=ADMIN).json()]


# ---------------------------------------------------------------- AC-3.2

def test_fill_edge_then_spill_to_fog_then_pending(client):
    edge, fog = Worker(client, "edge-1", "edge", slots=2), Worker(client, "fog-1", "fog", slots=1)
    edge.beat(), fog.beat()
    ids = [submit(client) for _ in range(4)]
    settle(client, [edge, fog], lambda: sum(job(client, j)["state"] != "PENDING" for j in ids) == 3)
    jobs = [job(client, j) for j in ids]
    assert [j["worker_id"] for j in jobs] == ["edge-1", "edge-1", "fog-1", None]
    assert [j["overflow"] for j in jobs] == [False, False, False, True]
    d = [j["decision"] for j in jobs]
    assert d[0]["policy"] == "LocalFirst" and d[0]["eligible"] == ["edge-1", "fog-1"]
    assert d[2]["eligible"] == ["fog-1"] and d[2]["ineligible"] == {"edge-1": "no_free_slot"}
    assert d[3]["worker_id"] is None and d[3]["ineligible"] == {"edge-1": "no_free_slot", "fog-1": "no_free_slot"}
    # A slot frees on Edge -> the waiting job goes there (LocalFirst), with a new decision row.
    edge.finish(ids[0])
    settle(client, [edge, fog], lambda: job(client, ids[3])["state"] == "ASSIGNED")
    assert job(client, ids[3])["worker_id"] == "edge-1"


# ---------------------------------------------------------------- AC-3.3

def test_burst_of_100_never_overbooks(client, tmp_path):
    edge = Worker(client, "edge-1", "edge", slots=2, budget=1536)
    fog = Worker(client, "fog-1", "fog", slots=2, budget=1536)
    edge.beat(), fog.beat()
    # Mixed sizes so the memory budget binds as well as the slots (748 MB x2 = 1496 <= 1536).
    sizes = [None, 700, 300, None, 700] * 20
    with ThreadPoolExecutor(16) as pool:
        ids = list(pool.map(lambda m: submit(client, m), sizes))
    assert len(set(ids)) == 100
    listed = lambda: client.get("/api/v1/jobs", params={"limit": 200}, headers=ADMIN).json()
    settle(client, [edge, fog], lambda: all(j["state"] in TERMINAL for j in listed()), timeout=60,
           auto_finish=True)
    assert {j["state"] for j in listed()} == {"SUCCEEDED"}
    peaks = audit(tmp_path / "dwr.db")
    for wid, p in peaks.items():
        assert p.violations == [], (wid, p.violations[:3])
        assert p.peak_slots == 2  # capacity was actually reached, so the check means something
        assert p.peak_mem_mb <= 1536


# ---------------------------------------------------------------- AC-3.4

def test_host_busy_enter_avoid_and_exit(client):
    edge, fog = Worker(client, "edge-1", "edge", slots=1), Worker(client, "fog-1", "fog", slots=2)
    edge.beat(), fog.beat()
    hold = submit(client)                                   # fills Edge's only slot
    settle(client, [edge, fog], lambda: job(client, hold)["state"] == "ASSIGNED")
    fog.foreign = 90.0
    for _ in range(10):                                     # fill the 10-sample window
        fog.beat()
    busy = events(client, "host_busy")
    assert len(busy) == 1 and busy[0]["worker_id"] == "fog-1" and busy[0]["details"]["mean_foreign_cpu_pct"] >= 70
    assert client.get("/api/v1/workers/fog-1", headers=ADMIN).json()["host_busy"] is True
    jid = submit(client)
    time.sleep(0.3)
    j = job(client, jid)
    assert j["state"] == "PENDING" and j["decision"]["ineligible"] == {"edge-1": "no_free_slot", "fog-1": "host_busy"}
    fog.foreign = 5.0
    settle(client, [edge, fog], lambda: job(client, jid)["state"] == "ASSIGNED")
    assert job(client, jid)["worker_id"] == "fog-1"
    free = events(client, "host_free")
    assert len(free) == 1 and free[0]["worker_id"] == "fog-1"


def test_host_busy_disabled_still_tracked_but_not_enforced(client):
    assert client.put("/api/v1/scheduler", json={"host_busy_enabled": False}, headers=ADMIN).status_code == 200
    fog = Worker(client, "fog-1", "fog")
    fog.foreign = 95.0
    for _ in range(5):
        fog.beat()
    assert events(client, "host_busy")[0]["details"]["enforced"] is False
    jid = submit(client)
    settle(client, [fog], lambda: job(client, jid)["state"] == "ASSIGNED")


# ---------------------------------------------------------------- AC-3.5

def test_too_big_for_everyone_rejected_too_big_for_edge_goes_to_fog(client):
    edge = Worker(client, "edge-1", "edge", slots=2, budget=512)
    fog = Worker(client, "fog-1", "fog", slots=2, budget=900)
    edge.beat(), fog.beat()
    big = submit(client, mem=1000)                          # declared 1040 > both budgets
    mid = submit(client, mem=600)                           # declared 640: Fog only
    settle(client, [edge, fog], lambda: job(client, big)["state"] == "REJECTED"
           and job(client, mid)["state"] == "ASSIGNED")
    d = job(client, big)["decision"]
    assert d["structural_reject"] and d["ineligible"] == {"edge-1": "mem_budget", "fog-1": "mem_budget"}
    m = job(client, mid)
    assert m["worker_id"] == "fog-1" and m["decision"]["ineligible"] == {"edge-1": "mem_budget"}
    assert not job(client, big)["overflow"]


# ---------------------------------------------------------------- PUT /scheduler, policies, aging

def test_put_scheduler_rules_and_persistence(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as c:
        assert c.get("/api/v1/scheduler", headers=ADMIN).json()["policy"] == "LocalFirst"
        assert c.put("/api/v1/scheduler", json={"policy": "Nope"}, headers=ADMIN).status_code == 422
        assert c.put("/api/v1/scheduler", json={"host_busy_low": 80}, headers=ADMIN).status_code == 422
        assert c.put("/api/v1/scheduler", json={"bogus": 1}, headers=ADMIN).status_code == 422
        edge, fog = Worker(c, "edge-1", "edge"), Worker(c, "fog-1", "fog")
        edge.beat(), fog.beat()
        jid = submit(c)
        r = c.put("/api/v1/scheduler", json={"policy": "EdgeOnly"}, headers=ADMIN)
        assert r.status_code == 409                          # a job is non-terminal
        settle(c, [edge, fog], lambda: job(c, jid)["state"] == "SUCCEEDED", auto_finish=True)
        r = c.put("/api/v1/scheduler", json={"policy": "EdgeOnly"}, headers=ADMIN)
        assert r.status_code == 200 and r.json()["policy"] == "EdgeOnly"
        a, b = submit(c), submit(c)                          # Edge has 1 slot; Fog is free but not used
        settle(c, [edge, fog], lambda: job(c, a)["state"] == "ASSIGNED")
        time.sleep(0.2)
        jb = job(c, b)
        assert jb["state"] == "PENDING" and jb["overflow"] and jb["decision"]["policy"] == "EdgeOnly"
        assert "fog-1" in jb["decision"]["eligible"]
        assert len(events(c, "scheduler_changed")) == 1
    with TestClient(create_app(make_settings(tmp_path))) as c:   # controller restart keeps the policy
        assert c.get("/api/v1/scheduler", headers=ADMIN).json()["policy"] == "EdgeOnly"


def test_aged_job_is_not_starved_by_later_small_jobs(client):
    assert client.put("/api/v1/scheduler", json={"aging_s": 0.3}, headers=ADMIN).status_code == 200
    edge = Worker(client, "edge-1", "edge", slots=3, budget=1064)
    edge.beat()
    small = submit(client)                                  # 48 MB, keeps running
    settle(client, [edge], lambda: job(client, small)["state"] == "ASSIGNED")
    big = submit(client, mem=1000)                          # 1048 MB: 48 + 1048 > 1064 -> waits
    time.sleep(0.5)                                         # big is now older than aging_s
    later = submit(client)                                  # would fit, but Edge is held for `big`
    time.sleep(0.3)
    lj = job(client, later)
    assert lj["state"] == "PENDING" and lj["decision"]["ineligible"] == {"edge-1": "reserved_for_aged"}
    time.sleep(0.5)                                         # `later` is aged now too: still held back
    assert job(client, later)["state"] == "PENDING"
    edge.finish(small)
    settle(client, [edge], lambda: job(client, big)["state"] == "ASSIGNED")
    assert job(client, later)["state"] == "PENDING"         # 1048 + 48 > 1064 until big ends
