import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from controller.blobs import BlobStore, generate

REPO = Path(__file__).resolve().parents[2]


def _start(type_, job_dir: Path, params: dict, cpus="0") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "worker.workloads", type_, "--job-id", "t", "--job-dir", str(job_dir),
         "--cpus", cpus, "--params", json.dumps(params)],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _run(type_, job_dir, params):
    p = _start(type_, job_dir, params)
    out, err = p.communicate(timeout=60)
    return p.returncode, err


def test_data_digest_hashes_input_rounds_times(tmp_path):
    data = generate(2 * 1024 * 1024 + 5)
    (tmp_path / "input.bin").write_bytes(data)
    rc, err = _run("data_digest", tmp_path, {"input_bytes": len(data), "rounds": 3})
    assert rc == 0, err
    r = json.loads((tmp_path / "result.json").read_text())
    d = b""
    for _ in range(3):
        d = hashlib.sha256(d + data).digest()
    assert r["checksum"] == d.hex() and r["bytes_hashed"] == 3 * len(data) and r["exec_s"] > 0


def test_data_digest_zero_bytes_needs_no_file(tmp_path):
    rc, err = _run("data_digest", tmp_path, {"input_bytes": 0, "rounds": 2})
    assert rc == 0, err


def test_data_digest_size_mismatch_fails(tmp_path):
    (tmp_path / "input.bin").write_bytes(b"short")
    rc, err = _run("data_digest", tmp_path, {"input_bytes": 1024, "rounds": 1})
    assert rc == 1 and "expected 1024" in err


def test_mem_touch_exec_includes_hold(tmp_path):
    rc, err = _run("mem_touch", tmp_path, {"mem_mb": 32, "hold_s": 0.6})
    assert rc == 0, err
    r = json.loads((tmp_path / "result.json").read_text())
    assert r["exec_s"] == pytest.approx(r["touch_s"] + r["hold_s_actual"])
    assert 0.6 <= r["hold_s_actual"] < 0.9 and r["exec_s"] >= 0.6
    assert r["peak_rss_mb"] >= 32 and r["allocated_mb"] == 32


def test_mem_touch_cancel_during_hold_is_prompt(tmp_path):
    p = _start("mem_touch", tmp_path, {"mem_mb": 16, "hold_s": 30})
    time.sleep(1.5)  # interpreter start + touch; now holding
    t0 = time.monotonic()
    (tmp_path / "cancel").touch()
    p.wait(timeout=10)
    assert p.returncode == 3
    assert time.monotonic() - t0 < 1.0  # contract: checked at least every 100 ms


def test_blob_store_is_deterministic_and_cached(tmp_path):
    store = BlobStore(tmp_path)
    sha = store.ensure(4096)
    assert sha == hashlib.sha256(generate(4096)).hexdigest()
    mtime = store.path(4096).stat().st_mtime_ns
    assert store.ensure(4096) == sha and store.path(4096).stat().st_mtime_ns == mtime
    assert generate(4096)[:100] == generate(100)  # SHAKE prefix property: sizes share a stream


# ---------------------------------------------------------------- supervisor input path

def _dd_spec(job_id, size=1024):
    return {"job_id": job_id, "type": "data_digest", "params": {"input_bytes": size, "rounds": 1},
            "input_bytes": size, "input_url": f"/api/v1/jobs/{job_id}/input", "input_sha256": "x"}


def _tick_until(sup, journal, job_id, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sup.tick()
        if journal.get(job_id)["state"] not in ("ACCEPTED", "RUNNING"):
            return journal.get(job_id)
        time.sleep(0.02)
    raise AssertionError("did not finish")


@pytest.fixture
def journal(tmp_path):
    from worker.journal import Journal
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def test_supervisor_downloads_then_spawns(journal, tmp_path):
    from worker.supervisor import Supervisor

    def fetch(job_id, spec, dest, cancel):
        dest.write_bytes(generate(spec["input_bytes"]))
        return spec["input_bytes"], 0.01

    sup = Supervisor(journal, [[0]], tmp_path / "jobs", fetch_input=fetch)
    journal.accept("d1", _dd_spec("d1"))
    row = _tick_until(sup, journal, "d1")
    assert row["state"] == "SUCCEEDED", row["error"]
    assert (row["transfer_bytes"], row["transfer_s"]) == (1024, 0.01)


def test_supervisor_download_failure(journal, tmp_path):
    from worker.supervisor import Supervisor

    def fetch(job_id, spec, dest, cancel):
        raise RuntimeError("HTTP 404")

    sup = Supervisor(journal, [[0]], tmp_path / "jobs", fetch_input=fetch)
    journal.accept("d1", _dd_spec("d1"))
    row = _tick_until(sup, journal, "d1")
    assert (row["state"], row["fail_reason"]) == ("FAILED", "input_transfer_error")
    assert "HTTP 404" in row["error"] and row["pid"] is None and sup.free_slots() == 1


def test_cancel_during_download_never_spawns_and_frees_slot(journal, tmp_path):
    from worker.supervisor import Supervisor, TransferCancelled
    started = threading.Event()

    def fetch(job_id, spec, dest, cancel):
        started.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise TransferCancelled()

    sup = Supervisor(journal, [[0]], tmp_path / "jobs", fetch_input=fetch)
    journal.accept("d1", _dd_spec("d1"))
    sup.tick()
    assert started.wait(5) and sup.free_slots() == 0
    sup.request_cancel("d1")
    end = time.monotonic() + 5
    while sup.free_slots() == 0 and time.monotonic() < end:
        sup.tick()
        time.sleep(0.01)
    row = journal.get("d1")
    assert row["state"] == "CANCELLED" and row["pid"] is None and sup.free_slots() == 1
