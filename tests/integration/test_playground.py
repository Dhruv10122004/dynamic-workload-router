"""Classroom playground (D-027/D-028) with scripted fake workers: "Why here?" cards, free play,
activities, live lanes and queue, story feed, the Fog-offline case, restart recovery."""
import time

import pytest
from fastapi.testclient import TestClient

from controller.main import create_app
from controller.settings import Settings
from shared.schemas import SCHEMA_VERSION
from tests.integration.test_scheduling import ADMIN, TOKENS

PG = "/api/v1/playground"


def make(tmp_path, **kw):
    s = Settings(worker_tokens=TOKENS, data_dir=tmp_path, admin_token="adm", scheduler_interval_s=0.05,
                 frontend_dist=None, **kw)
    return create_app(s)


@pytest.fixture
def client(tmp_path):
    with TestClient(make(tmp_path)) as c:
        yield c


class SlotWorker:
    """Fake agent that, like the real one, reports the slot and live stats of running jobs."""

    def __init__(self, c, wid, tier, slots=1):
        self.c, self.wid, self.seq, self.slots = c, wid, 0, slots
        self.headers = {"Authorization": f"Bearer {TOKENS[wid]}"}
        self.running: dict[str, int] = {}     # job_id -> slot
        self.done: list[str] = []
        body = {"schema_version": SCHEMA_VERSION, "worker_id": wid, "tier": tier, "hostname": f"{wid}-host",
                "os": "Linux", "os_version": "6", "python": "3.11", "cpu_logical": 8, "mem_total_mb": 16000,
                "slots": [{"slot": i, "cpus": [i + 2]} for i in range(slots)], "mem_budget_mb": 4000,
                "mem_floor_mb": 512, "supported_types": ["cpu_hash", "mem_touch", "data_digest"],
                "capabilities": {}, "agent_version": "t", "agent_instance_id": f"{wid}-inst"}
        assert c.post("/api/v1/workers/register", json=body, headers=self.headers).status_code == 200

    def beat(self):
        self.seq += 1
        jobs = [{"job_id": j, "state": "RUNNING", "slot": s, "pid": 4000 + s, "cpu_pct": 99.5, "rss_mb": 33.0}
                for j, s in self.running.items()]
        jobs += [{"job_id": j, "state": "SUCCEEDED", "result": {"exec_s": 0.5}} for j in self.done]
        body = {"schema_version": SCHEMA_VERSION, "agent_instance_id": f"{self.wid}-inst", "seq": self.seq,
                "telemetry": {"cpu_pct": 20, "own_cpu_pct": 10, "foreign_cpu_pct": 10, "mem_total_mb": 16000,
                              "mem_available_mb": 12000, "free_slots": 1}, "jobs": jobs}
        r = self.c.post(f"/api/v1/workers/{self.wid}/heartbeat", json=body, headers=self.headers)
        assert r.status_code == 200, r.text
        reply = r.json()
        self.done = [j for j in self.done if j not in reply["ack_terminal"]]
        for a in reply["assignments"]:
            free = sorted(set(range(self.slots)) - set(self.running.values()))
            self.running[a["job_id"]] = free[0]
        return reply

    def finish(self, job_id):
        del self.running[job_id]
        self.done.append(job_id)


def until(pred, timeout=5.0, beat=()):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in beat:
            w.beat()
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def card(c, jid):
    return c.get(f"{PG}/explain/{jid}", headers=ADMIN).json()


def click(c, task="big_hash", **kw):
    r = c.post(f"{PG}/task", json={"task_id": task, **kw}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_catalog_has_presets_tasks_and_labels(client):
    cat = client.get(f"{PG}/scenarios", headers=ADMIN).json()
    ids = {s["id"] for s in cat["scenarios"]}
    assert {"quiet", "saturate", "rush", "no_fog", "fog_busy", "both_busy", "disconnect", "cancel"} <= ids
    assert {t["id"] for t in cat["tasks"]} >= {"small_hash", "big_hash", "memory", "data", "long"}
    assert cat["labels"]["queue_title"] == "Cloud overflow queue"


def test_four_clicks_explain_edge_edge_fog_queue_then_pickup(client):
    edge, fog = SlotWorker(client, "edge-1", "edge", slots=2), SlotWorker(client, "fog-1", "fog", slots=1)
    edge.beat(), fog.beat()
    ids = [click(client)["jobs"][0]["job_id"] for _ in range(4)]
    until(lambda: all(card(client, j)["headline"] and "waiting for the scheduler" not in card(client, j)["headline"]
                      for j in ids))
    cards = [card(client, j) for j in ids]
    assert cards[0]["headline"].startswith("Your 1st click (hash 2M)") and "edge-1 (edge) took it" in cards[0]["headline"]
    assert "edge-1 (edge) took it" in cards[1]["headline"]
    assert "edge-1: all its slots are busy, so fog-1 (fog) took it" in cards[2]["headline"]
    assert "Cloud overflow queue" in cards[3]["headline"]
    assert "edge-1: all its slots are busy · fog-1: all its slots are busy" in cards[3]["headline"]
    until(lambda: len(edge.running) == 2 and len(fog.running) == 1, beat=[edge, fog])
    edge.finish(ids[0])
    until(lambda: any("Picked from the queue by edge-1" in l for l in card(client, ids[3])["lines"]), beat=[edge, fog])
    until(lambda: card(client, ids[0])["state"] == "SUCCEEDED", beat=[edge, fog])
    assert any(l.startswith("Finished: SUCCEEDED") for l in card(client, ids[0])["lines"])
    feed = client.get(f"{PG}/live", headers=ADMIN).json()["feed"]
    texts = [f["text"] for f in feed]
    assert any("edge-1 full → #3 hash 2M → fog-1" in t for t in texts), texts
    assert any("#4 hash 2M picked from the queue by edge-1" in t for t in texts), texts


def test_free_play_run_opens_on_first_click_and_is_reused(client):
    w = SlotWorker(client, "edge-1", "edge")
    w.beat()
    a = click(client)["activity"]
    b = click(client, "small_hash")["activity"]
    assert a["kind"] == "free_play" and a["run_id"] == b["run_id"] and a["running"]
    runs = client.get("/api/v1/runs", params={"active": "true"}, headers=ADMIN).json()
    assert [r["run_id"] for r in runs] == [a["run_id"]]
    # a preset can't start while free-play jobs are still running...
    assert client.post(f"{PG}/activity", json={"scenario": "quiet"}, headers=ADMIN).status_code == 409
    # ...but once they are done, starting a preset closes free play and opens a new run
    until(lambda: len(w.running) >= 1, beat=[w])
    for _ in range(4):
        for j in list(w.running):
            w.finish(j)
        w.beat()
    until(lambda: not client.get("/api/v1/jobs", params={"state": "PENDING"}, headers=ADMIN).json()
          and not w.running, beat=[w])
    r = client.post(f"{PG}/activity", json={"scenario": "quiet"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["title"] == "Quiet day" and r.json()["run_id"] != a["run_id"]
    client.post(f"{PG}/activity/stop", headers=ADMIN)


def test_custom_activity_validation_start_and_stop(client):
    edge = SlotWorker(client, "edge-1", "edge", slots=1)
    edge.beat()
    bad = client.post(f"{PG}/activity", json={"custom": {"load": 9, "duration_s": 5}}, headers=ADMIN)
    assert bad.status_code == 422
    r = client.post(f"{PG}/activity", json={"custom": {"load": 3.0, "duration_s": 30, "mix": "cpu"}}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["kind"] == "custom" and r.json()["total"] > 5
    until(lambda: client.get(f"{PG}/activity", headers=ADMIN).json()["submitted"] >= 2, beat=[edge])
    s = client.post(f"{PG}/activity/stop", headers=ADMIN).json()
    assert s["running"] is False
    run_id = s["run_id"]

    def all_done():
        jobs = client.get("/api/v1/jobs", params={"run_id": run_id, "limit": 500}, headers=ADMIN).json()
        return jobs and all(j["state"] in ("SUCCEEDED", "CANCELLED", "KILLED") for j in jobs)

    for _ in range(40):                     # running jobs end only when the (fake) worker reports
        for j in list(edge.running):
            edge.finish(j)
        edge.beat()
        if all_done():
            break
        time.sleep(0.1)
    assert all_done()
    until(lambda: client.get(f"/api/v1/runs/{run_id}", headers=ADMIN).json()["end_ts"] is not None, beat=[edge])


def test_live_lanes_slots_stats_queue_and_offline_tile(tmp_path):
    with TestClient(make(tmp_path, stale_s=0.5, maintenance_interval_s=0.1)) as c:
        edge, fog = SlotWorker(c, "edge-1", "edge", slots=1), SlotWorker(c, "fog-1", "fog", slots=1)
        edge.beat(), fog.beat()
        ids = [click(c, "long")["jobs"][0]["job_id"] for _ in range(3)]
        until(lambda: len(edge.running) == 1 and len(fog.running) == 1, beat=[edge, fog])
        edge.beat(), fog.beat()
        live = c.get(f"{PG}/live", headers=ADMIN).json()
        lanes = {w["worker_id"]: w for w in live["workers"]}
        assert [w["worker_id"] for w in live["workers"]] == ["edge-1", "fog-1"]      # by tier rank
        tile = lanes["edge-1"]["slots"][0]["job"]
        assert tile["job_id"] == ids[0] and tile["cores"] == [2] and tile["cpu_pct"] == 99.5 and tile["rss_mb"] == 33.0
        assert [q["job_id"] for q in live["queue"]] == [ids[2]]
        assert live["labels"]["queue_title"] == "Cloud overflow queue"
        # Fog drops off: its running job stays on Fog as an offline tile and is never queued
        until(lambda: c.get("/api/v1/jobs/" + ids[1], headers=ADMIN).json()["state"] == "UNKNOWN", 3, beat=[edge])
        live = c.get(f"{PG}/live", headers=ADMIN).json()
        fog_lane = {w["worker_id"]: w for w in live["workers"]}["fog-1"]
        assert fog_lane["stale"] and fog_lane["slots"][0]["job"]["offline"] is True
        assert ids[1] not in [q["job_id"] for q in live["queue"]]
        assert "still running there, result pending" in " ".join(card(c, ids[1])["lines"])
        assert any("fog-1 lost contact" in f["text"] for f in live["feed"])
        # reconnect: the result arrives and the reply hands Fog the queued job
        fog.finish(ids[1])
        reply = fog.beat()
        assert [a["job_id"] for a in reply["assignments"]] == [ids[2]]
        assert c.get("/api/v1/jobs/" + ids[1], headers=ADMIN).json()["state"] == "SUCCEEDED"
        texts = [f["text"] for f in c.get(f"{PG}/live", headers=ADMIN).json()["feed"]]
        assert any("result arrived after fog-1 reconnected: SUCCEEDED" in t for t in texts), texts
        assert any("#3 memory 64 MB · 30 s picked from the queue by fog-1" in t for t in texts), texts


def test_restart_closes_an_open_playground_run_and_cancels_its_jobs(tmp_path):
    with TestClient(make(tmp_path)) as c:
        SlotWorker(c, "edge-1", "edge").beat()
        res = click(c, count=3)                      # 1 slot: one assigned, two waiting
        rid = res["activity"]["run_id"]
        ids = [j["job_id"] for j in res["jobs"]]
    with TestClient(make(tmp_path)) as c:
        assert c.get(f"/api/v1/runs/{rid}", headers=ADMIN).json()["end_ts"] is not None
        assert c.get("/api/v1/runs", params={"active": "true"}, headers=ADMIN).json() == []
        jobs = [c.get(f"/api/v1/jobs/{j}", headers=ADMIN).json() for j in ids]
        assert not [j for j in jobs if j["state"] == "PENDING"]          # nothing left in the queue
        assert all(j["state"] == "CANCELLED" or j["cancel_requested"] for j in jobs)
