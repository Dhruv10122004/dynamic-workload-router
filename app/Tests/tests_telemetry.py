import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_stats_endpoint_structure():
    """Verifies that /stats returns 200 and the exact expected keys."""
    response = client.get("/stats")
    assert response.status_code == 200
    
    data = response.json()
    expected_keys = {"tier", "cpu_percent", "active_requests", "avg_latency_ms", "uptime_seconds"}
    assert expected_keys.issubset(data.keys())
    assert isinstance(data["active_requests"], int)
    assert isinstance(data["avg_latency_ms"], (int, float))

def test_workload_updates_latency_window():
    """Verifies running a workload calculates latency and updates /stats."""
    # 1. Trigger a fast workload
    work_resp = client.post("/workload?iterations=5000")
    assert work_resp.status_code == 200
    assert "compute_time_seconds" in work_resp.json()

    # 2. Check /stats to verify avg_latency_ms recorded the run
    stats_resp = client.get("/stats")
    assert stats_resp.status_code == 200
    stats_data = stats_resp.json()
    assert stats_data["avg_latency_ms"] > 0.0
    assert stats_data["active_requests"] == 0  # Should be back to 0 after finishing