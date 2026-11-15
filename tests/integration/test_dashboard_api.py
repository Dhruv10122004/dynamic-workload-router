"""M5: the API the dashboard reads (overview, decisions, newest-first lists), same-origin
serving of the build, and AC-5.1's "no hardcoded tiers or metrics" check."""
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller.main import create_app
from controller.overview import backlog_stats
from controller.settings import Settings
from tests.integration.test_scheduling import ADMIN, TOKENS, Worker, job, settle, submit

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(tmp_path):
    s = Settings(worker_tokens=TOKENS, data_dir=tmp_path, admin_token="adm", scheduler_interval_s=0.05,
                 frontend_dist=None)
    with TestClient(create_app(s)) as c:
        yield c


def test_backlog_stats_replay():
    ev = lambda ts, src, dst: {"ts": ts, "from_state": src, "to_state": dst}
    events = [ev(0, None, "PENDING"), ev(1, None, "PENDING"), ev(2, "PENDING", "ASSIGNED"),
              ev(3, "PENDING", "ASSIGNED"), ev(10, None, "PENDING"), ev(12, "PENDING", "CANCELLED"),
              ev(20, None, "PENDING")]
    peak, busy = backlog_stats(events, now=25)
    assert peak == 2 and busy == 3 + 2 + 5   # [0,3] + [10,12] + [20,now=25]


def test_overview_counts_tiers_offload_and_overflow(client):
    edge, fog = Worker(client, "edge-1", "edge", slots=1), Worker(client, "fog-1", "fog", slots=1)
    edge.beat(), fog.beat()
    ids = [submit(client) for _ in range(3)]               # edge, fog, waits (overflow)
    settle(client, [edge, fog], lambda: sum(job(client, j)["state"] == "RUNNING" for j in ids) == 2)
    o = client.get("/api/v1/overview", headers=ADMIN).json()
    assert o["total_jobs"] == 3 and o["backlog"] == 1 and o["overflow_jobs"] == 1 and o["peak_backlog"] >= 1
    assert o["by_state"] == {"PENDING": 1, "RUNNING": 2}
    assert list(o["tiers"]) == ["edge", "fog"] and o["first_tier"] == "edge"
    settle(client, [edge, fog], lambda: all(job(client, j)["state"] == "SUCCEEDED" for j in ids), auto_finish=True)
    o = client.get("/api/v1/overview", headers=ADMIN).json()
    assert o["completed"] == 3 and o["backlog"] == 0 and o["overflow_time_s"] > 0
    placed = {t: v["completed"] for t, v in o["tiers"].items()}
    assert sum(placed.values()) == 3 and placed["fog"] >= 1
    assert o["local_offload_pct_by_count"] == round(100 * placed["fog"] / 3, 1)
    assert o["scheduler"]["policy"] == "LocalFirst"


def test_overview_by_run_id(client):
    fog = Worker(client, "fog-1", "fog")
    fog.beat()
    submit(client, run_id="r1")
    submit(client, run_id="r2")
    assert client.get("/api/v1/overview", params={"run_id": "r1"}, headers=ADMIN).json()["total_jobs"] == 1


def test_decisions_and_newest_first_lists(client):
    fog = Worker(client, "fog-1", "fog")
    fog.beat()
    a = submit(client)
    b = submit(client)
    settle(client, [fog], lambda: job(client, a)["state"] == "RUNNING")
    d = client.get("/api/v1/decisions", headers=ADMIN).json()
    assert [x["job_id"] for x in d][:2] in ([b, a], [a, b]) and d[0]["id"] > d[1]["id"]
    assert {"type", "job_state", "eligible", "ineligible", "policy", "decision_us"} <= set(d[0])
    jobs = client.get("/api/v1/jobs", params={"order": "desc", "limit": 1}, headers=ADMIN).json()
    assert [j["job_id"] for j in jobs] == [b]
    client.post("/api/v1/workers/fog-1/drain", headers=ADMIN)   # a second event
    ev = client.get("/api/v1/events", params={"order": "desc", "limit": 2}, headers=ADMIN).json()
    assert ev[0]["id"] > ev[1]["id"]
    assert client.get("/api/v1/jobs", params={"order": "sideways"}, headers=ADMIN).status_code == 422


def test_build_is_served_on_the_same_origin(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>dashboard</html>")
    (dist / "assets" / "x.js").write_text("// js")
    s = Settings(worker_tokens=TOKENS, data_dir=tmp_path / "ctl", admin_token="adm", frontend_dist=dist)
    with TestClient(create_app(s)) as c:
        assert "dashboard" in c.get("/").text
        assert c.get("/assets/x.js").status_code == 200
        assert c.get("/api/v1/health").json()["status"] in ("READY", "DEGRADED")   # API still wins
        assert c.get("/api/v1/workers", headers=ADMIN).status_code == 200


# ---------------------------------------------------------------- AC-5.1

FORBIDDEN = [
    (r"\b(edge|fog|cloud)\b", "hardcoded tier name"),
    (r"\b(p99|p95|rps|score|cost_score)\b", "hardcoded metric"),
    (r"API_BASE|https?://|127\.0\.0\.1|localhost|:8000", "fixed API address"),
    (r"Math\.random", "fabricated value"),
]


def test_frontend_src_has_no_hardcoded_tiers_metrics_or_addresses():
    src = REPO / "frontend" / "src"
    hits = []
    for f in sorted(src.rglob("*")):
        if f.suffix not in (".js", ".jsx", ".css", ".ts", ".tsx"):
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for pat, why in FORBIDDEN:
                if re.search(pat, line, re.IGNORECASE):
                    hits.append(f"{f.relative_to(REPO)}:{n}: {why}: {line.strip()[:100]}")
    assert not hits, "\n".join(hits)
