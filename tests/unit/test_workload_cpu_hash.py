import hashlib
import json
import subprocess
import sys
from pathlib import Path

from worker.workloads.cpu_hash import SEED

REPO = Path(__file__).resolve().parents[2]


def _run(job_dir: Path, params: dict, cpus: str = "0") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "worker.workloads", "cpu_hash", "--job-id", "t1", "--job-dir", str(job_dir),
         "--cpus", cpus, "--params", json.dumps(params)],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )


def _expected_checksum(n: int) -> str:
    d = SEED
    for _ in range(n):
        d = hashlib.sha256(d).digest()
    return d.hex()


def test_success_writes_complete_result(tmp_path):
    p = _run(tmp_path, {"iterations": 20_000})
    assert p.returncode == 0, p.stderr
    r = json.loads((tmp_path / "result.json").read_text())
    assert r["checksum"] == _expected_checksum(20_000)
    assert r["exec_s"] > 0 and r["cpu_s"] > 0 and r["peak_rss_mb"] > 0 and r["pid"] > 0
    assert not (tmp_path / "result.json.tmp").exists()


def test_invalid_params_exit_2(tmp_path):
    p = _run(tmp_path, {"iterations": 5})
    assert p.returncode == 2
    assert not (tmp_path / "result.json").exists()


def test_cancel_sentinel_exit_3(tmp_path):
    (tmp_path / "cancel").touch()
    p = _run(tmp_path, {"iterations": 5_000_000})
    assert p.returncode == 3
    assert not (tmp_path / "result.json").exists()


def test_bad_cpu_list_is_an_error_not_a_crash(tmp_path):
    p = _run(tmp_path, {"iterations": 10_000}, cpus="9999")
    assert p.returncode == 1
