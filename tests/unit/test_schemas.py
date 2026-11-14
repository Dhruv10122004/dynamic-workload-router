import pytest

from shared.catalog import CATALOG, MIB
from shared.envfile import load_env
from shared.schemas import Assignment, JobValidationError, validate_assignment, validate_job


def test_cpu_hash_defaults_and_declared_memory():
    v = validate_job("cpu_hash", {})
    assert v.params == {"iterations": 1_000_000}
    assert v.mem_mb == 40 and v.max_runtime_s == 120 and v.timeout_s is None


@pytest.mark.parametrize("params", [
    {"iterations": 9_999}, {"iterations": 5_000_001}, {"iterations": 1.5},
    {"iterations": "100000"}, {"iterations": True}, {"iterations": 100_000, "extra": 1},
])
def test_cpu_hash_rejects_out_of_bounds_or_wrong_type(params):
    with pytest.raises(JobValidationError):
        validate_job("cpu_hash", params)


def test_unknown_type_rejected():
    with pytest.raises(JobValidationError, match="unknown workload type"):
        validate_job("rm_rf", {})


def test_timeout_must_not_exceed_max_runtime():
    assert validate_job("cpu_hash", {}, 120).timeout_s == 120
    for bad in (0, -1, 120.5):
        with pytest.raises(JobValidationError):
            validate_job("cpu_hash", {}, bad)


def test_data_digest_and_mem_touch_declared_memory_and_runtime():
    v = validate_job("data_digest", {"input_bytes": 10 * MIB + 1, "rounds": 2})
    assert v.mem_mb == 40 + 11
    m = validate_job("mem_touch", {"mem_mb": 256, "hold_s": 10})
    assert m.mem_mb == 296 and m.max_runtime_s == 70.0
    assert validate_job("mem_touch", {"mem_mb": 16, "hold_s": 2.5}).params["hold_s"] == 2.5


def test_test_only_param_needs_test_mode():
    p = {"mem_mb": 64, "hold_s": 1, "actual_mb_override": 512}
    with pytest.raises(JobValidationError, match="test-only"):
        validate_job("mem_touch", p)
    assert validate_job("mem_touch", p, test_mode=True).params["actual_mb_override"] == 512


def test_catalog_json_lists_all_types():
    assert {wt.to_json()["type"] for wt in CATALOG.values()} == {"cpu_hash", "data_digest", "mem_touch"}


def _assignment(**kw):
    base = dict(job_id="j1", type="cpu_hash", params={"iterations": 20_000}, max_runtime_s=120, mem_mb=40)
    return Assignment(**{**base, **kw})


def test_worker_side_validation():
    assert validate_assignment(_assignment()).params == {"iterations": 20_000}
    for bad in (_assignment(params={"iterations": 5}), _assignment(mem_mb=1),
                _assignment(max_runtime_s=999), _assignment(timeout_s=500)):
        with pytest.raises(JobValidationError):
            validate_assignment(bad)


def test_env_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nDWR_A=1  # note\nDWR_B='x y'\nOTHER=z\n", encoding="utf-8")
    monkeypatch.setenv("DWR_B", "from-env")
    env = load_env(f)
    assert env["DWR_A"] == "1" and env["DWR_B"] == "from-env" and env["OTHER"] == "z"
    with pytest.raises(FileNotFoundError):
        load_env(tmp_path / "missing")
