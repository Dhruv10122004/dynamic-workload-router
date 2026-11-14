import json

import httpx
import pytest

from shared.schemas import HeartbeatReply
from worker.agent import Agent, AgentConfig, ConfigError, parse_slot_cpus
from worker.telemetry import foreign_cpu_pct

ENV = {
    "DWR_WORKER_ID": "fog-1", "DWR_TIER": "fog", "DWR_CONTROLLER_URL": "http://ctl:8000/",
    "DWR_WORKER_TOKEN": "t", "DWR_SLOT_CPUS": "0", "DWR_MEM_BUDGET_MB": "1536", "DWR_MEM_FLOOR_MB": "512",
}


def test_parse_slot_cpus():
    assert parse_slot_cpus("1;2") == [[1], [2]]
    assert parse_slot_cpus("1,2;3,4") == [[1, 2], [3, 4]]
    for bad in ("", "1;;2", "1;1", "a"):
        with pytest.raises(ValueError):
            parse_slot_cpus(bad)


def test_config_lists_every_problem():
    with pytest.raises(ConfigError) as e:
        AgentConfig.from_env({**ENV, "DWR_TIER": "cloud", "DWR_WORKER_TOKEN": "", "DWR_MEM_BUDGET_MB": "x",
                              "DWR_SLOT_CPUS": "99999"})
    msg = str(e.value)
    for key in ("DWR_TIER", "DWR_WORKER_TOKEN", "DWR_MEM_BUDGET_MB", "DWR_SLOT_CPUS"):
        assert key in msg
    assert AgentConfig.from_env(ENV).controller_url == "http://ctl:8000"


def test_foreign_cpu():
    assert foreign_cpu_pct(80.0, 30.0) == 50.0
    assert foreign_cpu_pct(20.0, 30.0) == 0.0  # sampling skew never goes negative


@pytest.fixture
def agent(tmp_path):
    sent = []

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"seq_ack": sent[-1]["seq"], "server_processing_ms": 0.1,
                                         "drain": False, "assignments": [], "cancel": [], "ack_terminal": []})

    cfg = AgentConfig.from_env({**ENV, "DWR_DATA_DIR": str(tmp_path)})
    a = Agent(cfg, client=httpx.Client(base_url="http://ctl/api/v1", transport=httpx.MockTransport(handler)))
    a.sent = sent
    yield a
    a.journal.close()


def _assign(job_id, **kw):
    return {"job_id": job_id, "type": "cpu_hash", "params": {"iterations": 20_000}, "max_runtime_s": 120,
            "mem_mb": 40, **kw}


def _reply(**kw):
    return HeartbeatReply.model_validate({"seq_ack": 1, "server_processing_ms": 0, "drain": False, **kw})


def test_apply_reply_journals_assignments_once(agent):
    agent.apply_reply(_reply(assignments=[_assign("a")]))
    agent.apply_reply(_reply(assignments=[_assign("a")]))  # lost reply re-delivered
    assert [r["job_id"] for r in agent.journal.snapshot_rows()] == ["a"]
    assert agent.journal.get("a")["state"] == "ACCEPTED"


def test_worker_side_validation_bypassing_controller(agent):
    agent.apply_reply(_reply(assignments=[_assign("bad", params={"iterations": 5}),
                                          _assign("nope", type="data_digest", mem_mb=41, input_bytes=1024,
                                                  params={"input_bytes": 1024, "rounds": 1})]))  # no input_url
    for j in ("bad", "nope"):
        row = agent.journal.get(j)
        assert (row["state"], row["fail_reason"]) == ("FAILED", "worker_validation")


def test_assignment_and_cancel_in_same_reply(agent):
    agent.apply_reply(_reply(assignments=[_assign("c")], cancel=["c"]))
    assert agent.journal.get("c")["state"] == "CANCELLED"


def test_heartbeat_seq_increases_and_ack_removes_terminal(agent):
    agent.apply_reply(_reply(assignments=[_assign("x")], cancel=["x"]))
    assert agent.heartbeat_once() and agent.heartbeat_once()
    assert [p["seq"] for p in agent.sent] == [1, 2]
    assert agent.sent[-1]["jobs"][0]["state"] == "CANCELLED"
    assert agent.sent[-1]["telemetry"]["free_slots"] == 1
    agent.apply_reply(_reply(ack_terminal=["x"]))
    agent.heartbeat_once()
    assert agent.sent[-1]["jobs"] == []
