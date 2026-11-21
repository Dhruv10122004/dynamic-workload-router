"""Playground pure parts: presets are deterministic and comparable; labels; custom bounds."""
import pytest

from controller.playground import SCENARIOS, TASKS, Playground, job_label
from controller.service import Invalid
from shared.catalog import check_params


class FakeService:
    def list_workers(self):
        return [{"worker_id": "edge-1", "tier": "edge", "slots_total": 2, "fresh": True},
                {"worker_id": "fog-1", "tier": "fog", "slots_total": 1, "fresh": True}]


def scenario(sid):
    return next(s for s in SCENARIOS if s["id"] == sid)


def test_presets_are_deterministic_and_no_fog_replays_saturate():
    pg = Playground(FakeService())
    a, b = pg._rows(scenario("saturate")), pg._rows(scenario("saturate"))
    assert a == b and len(a) > 10
    assert pg._rows(scenario("no_fog")) == a          # same jobs, other policy: a fair comparison
    assert pg._rows(scenario("rush")) != a


def test_every_preset_and_task_is_a_valid_job():
    pg = Playground(FakeService())
    for s in SCENARIOS:
        for r in pg._rows(s):
            check_params(r["type"], r["params"])     # raises if out of the catalog's bounds
    for t in TASKS:
        check_params(t["type"], t["params"])


def test_cancel_preset_pins_one_long_job_per_fresh_worker():
    rows = Playground(FakeService())._rows(scenario("cancel"))
    assert [r["placement"] for r in rows] == ["edge-1", "fog-1"] and all(r["t_s"] == 0 for r in rows)


def test_custom_bounds():
    pg = Playground(FakeService())
    assert pg._custom({"load": 1.0, "duration_s": 60})["policy"] == "LocalFirst"
    for bad in ({"load": 5}, {"duration_s": 10}, {"mix": "nope"}, {"policy": "Nope"}, {"load": "x"}):
        with pytest.raises(Invalid):
            pg._custom(bad)


def test_job_labels():
    assert job_label("cpu_hash", {"iterations": 2_000_000}) == "hash 2M"
    assert job_label("mem_touch", {"mem_mb": 256, "hold_s": 10.0}) == "memory 256 MB · 10 s"
    assert job_label("data_digest", {"input_bytes": 16 * 1024 * 1024, "rounds": 2}) == "data 16 MB × 2"
