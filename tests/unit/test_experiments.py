"""M6 harness, pure parts: trace generation (AC-6.1), sweep plan, analysis maths (AC-6.2)."""
import csv
import json

import pytest

from experiments import analyze
from experiments.gen_trace import (
    MIXES, VARIANTS, capacity_jobs_per_s, generate, mean_time, read_trace, to_csv, variant_key, write_trace,
)
from experiments.sweep import plan_runs

REF = {"edge_worker": "edge-1", "edge_slots": 2,
       "mean_service_s": {variant_key(t, p): 1.0 + i * 0.5 for t in VARIANTS for i, p in enumerate(VARIANTS[t])},
       "mean_exec_s": {variant_key(t, p): 0.5 + i * 0.25 for t in VARIANTS for i, p in enumerate(VARIANTS[t])}}


# ---------------------------------------------------------------- AC-6.1

def test_same_seed_same_bytes_and_hash(tmp_path):
    kw = dict(reference=REF, load=1.5, mix="mixed", arrival="poisson", duration_s=120, seed=7)
    h1 = write_trace(tmp_path / "a.csv", generate(**kw), {})
    h2 = write_trace(tmp_path / "b.csv", generate(**kw), {})
    assert h1 == h2 and (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()
    assert write_trace(tmp_path / "c.csv", generate(**{**kw, "seed": 8}), {}) != h1
    rows, h = read_trace(tmp_path / "a.csv")
    assert h == h1 and rows == generate(**kw)   # replay reads back exactly what was generated


def test_rate_matches_load_and_reference():
    # E[exec] for cpu = mean(1.0, 1.5, 2.0) = 1.5 s; 2 slots -> 1.333 jobs/s at load 1
    assert mean_time(REF, "cpu") == pytest.approx(1.5)
    assert capacity_jobs_per_s(REF, "cpu") == pytest.approx(2 / 1.5)
    assert capacity_jobs_per_s(REF, "cpu", "exec_s") == pytest.approx(2 / 0.75)   # the plan's literal basis
    rows = generate(reference=REF, load=2.0, mix="cpu", arrival="poisson", duration_s=3000, seed=1)
    assert len(rows) / 3000 == pytest.approx(2 * 2 / 1.5, rel=0.05)
    assert all(0 <= r["t_s"] < 3000 for r in rows) and [r["t_s"] for r in rows] == sorted(r["t_s"] for r in rows)


def test_bursts_only_in_on_phases_with_same_mean_rate():
    rows = generate(reference=REF, load=1.0, mix="cpu", arrival="bursts", duration_s=4000, seed=3)
    assert all(r["t_s"] % 40 < 20 for r in rows)                 # 20 s on / 20 s off
    assert len(rows) / 4000 == pytest.approx(2 / 1.5, rel=0.05)


def test_mixed_proportions_and_sizes_come_from_variants():
    rows = generate(reference=REF, load=3.0, mix="mixed", arrival="poisson", duration_s=3000, seed=2)
    share = {t: sum(r["type"] == t for r in rows) / len(rows) for t in MIXES["mixed"]}
    for t, w in MIXES["mixed"].items():
        assert share[t] == pytest.approx(w, abs=0.03)
    assert all(r["params"] in VARIANTS[r["type"]] for r in rows)
    assert to_csv(rows).startswith(b"index,t_s,type,params_json\n")


def test_sweep_plan_rotates_and_shares_traces():
    cfg = {"strategies": [{"name": "A"}, {"name": "B"}, {"name": "C"}], "reps": 3, "loads": [1, 2], "base_seed": 10}
    runs = plan_runs(cfg)
    assert len(runs) == 18 and len({r["key"] for r in runs}) == 18
    first_of_rep = [next(r["strategy"]["name"] for r in runs if r["rep"] == k) for k in range(3)]
    assert first_of_rep == ["A", "B", "C"]
    seeds = {(r["load"], r["rep"]): r["seed"] for r in runs}
    assert all(r["seed"] == seeds[(r["load"], r["rep"])] for r in runs)   # same trace for every strategy
    assert len(set(seeds.values())) == 6


# ---------------------------------------------------------------- analysis (AC-6.2)

JOB_COLS = ["job_id", "type", "params_json", "state", "tier", "worker_id", "overflow", "submit_ts", "assign_ts",
            "end_ts", "completion_s", "queue_wait_s", "exec_s", "cpu_s", "transfer_bytes"]


def _job(i, state, tier, submit, assign, end, overflow=0, exec_s=1.0, type_="cpu_hash", params=None):
    params = params or VARIANTS["cpu_hash"][0]
    return {"job_id": f"j{i}", "type": type_, "params_json": json.dumps(params, sort_keys=True), "state": state,
            "tier": tier or "", "worker_id": {"edge": "edge-1", "fog": "fog-1"}.get(tier, ""), "overflow": overflow,
            "submit_ts": submit, "assign_ts": "" if assign is None else assign, "end_ts": "" if end is None else end,
            "completion_s": "" if end is None else end - submit,
            "queue_wait_s": "" if assign is None else assign - submit,
            "exec_s": "" if exec_s is None else exec_s, "cpu_s": "" if exec_s is None else exec_s,
            "transfer_bytes": ""}


def make_run(root, name, strategy, load, rep, jobs, start=100.0, end=300.0, drained=()):
    d = root / name
    d.mkdir(parents=True)
    workers = [{"worker_id": "edge-1", "tier": "edge"}, {"worker_id": "fog-1", "tier": "fog"}]
    manifest = {"run": {"run_id": name, "start_ts": start, "end_ts": end,
                        "config": {"workers": workers, "sweep": {"strategy": strategy, "load": load, "rep": rep}}},
                "trace": {"sha256": "x", "load": load}, "strategy": {"policy": strategy, "host_busy_enabled": True},
                "run_length_s": end - start, "cancelled_at_drain": list(drained), "network": "LAN",
                "git_commit": "abc", "reference": {"mean_exec_s": REF["mean_exec_s"]}, "median_rtt_ms": {}}
    (d / "manifest.json").write_text(json.dumps(manifest))
    with open(d / "jobs.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOB_COLS)
        w.writeheader()
        w.writerows(jobs)
    return d


def test_run_metrics_definitions(tmp_path):
    jobs = [
        _job(0, "SUCCEEDED", "edge", 100, 100, 102),                    # completion 2
        _job(1, "SUCCEEDED", "fog", 101, 101, 105, exec_s=3.0),         # completion 4, offloaded
        _job(2, "SUCCEEDED", "edge", 101, 106, 110, overflow=1),        # waited 5 s, completion 9
        _job(3, "FAILED", None, 102, None, 402, overflow=1, exec_s=None),  # queue_timeout: never ran
        _job(4, "CANCELLED", None, 103, None, 300, overflow=1, exec_s=None),  # cancelled at drain
        _job(5, "KILLED", "edge", 104, 104, 300, exec_s=None),            # running at drain end: killed
    ]
    d = make_run(tmp_path, "r1", "LocalFirst", 1.0, 0, jobs, drained=["j4", "j5"])
    m = analyze.run_metrics(analyze.load_run(d))
    assert m["jobs"] == 6 and m["completed"] == 3 and m["failed"] == 1 and m["unfinished"] == 2
    assert m["failure_rate"] == pytest.approx(1 / 6)
    assert m["mean_completion_s"] == pytest.approx(5.0)
    assert m["offload_pct_count"] == pytest.approx(100 / 3)
    assert m["offload_pct_cpu_s"] == pytest.approx(100 * 3 / 5)
    # overflow demand: job 2 ran (1.0 s); jobs 3 and 4 never ran -> reference exec_s of their size (0.5 s each)
    assert m["overflow_jobs"] == 3 and m["overflow_demand_s"] == pytest.approx(2.0)
    assert m["overflow_demand_estimated"] == 2
    # PENDING intervals: j2 [101,106], j3 [102,402], j4 [103,300] -> peak 3, non-empty 101..402
    assert m["peak_backlog"] == 3 and m["overflow_time_s"] == pytest.approx(301)


def test_backlog_exits_before_entries_at_same_instant():
    jobs = [{"submit_ts": "0", "assign_ts": "5", "end_ts": ""}, {"submit_ts": "5", "assign_ts": "6", "end_ts": ""}]
    assert analyze.backlog(jobs, run_end=10) == (1, 6)


def test_block_summary_slo_headline_and_outputs(tmp_path):
    block = tmp_path / "sweep"
    fast = lambda i: _job(i, "SUCCEEDED", "edge", 0, 0, 2)
    slow = lambda i: _job(i, "SUCCEEDED", "edge", 0, 0, 10)
    for rep in range(2):
        make_run(block, f"lf1-{rep}", "LocalFirst", 1.0, rep, [fast(0), fast(1)])
        make_run(block, f"lf2-{rep}", "LocalFirst", 2.0, rep, [fast(0), _job(1, "SUCCEEDED", "fog", 0, 0, 2.5)])
        make_run(block, f"eo1-{rep}", "EdgeOnly", 1.0, rep, [fast(0), _job(1, "SUCCEEDED", "edge", 0, 4, 6, overflow=1, exec_s=2.0)])
        make_run(block, f"eo2-{rep}", "EdgeOnly", 2.0, rep, [slow(0), _job(1, "SUCCEEDED", "edge", 0, 8, 10, overflow=1, exec_s=4.0)])
    assert analyze.main([str(block)]) == 0
    out = block / "analysis"
    summary = list(csv.DictReader(open(out / "summary.csv")))
    assert len(summary) == 4 and all(s["reps"] == "2" for s in summary)
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "LocalFirst: acceptable up to load L* = 2.0" in report
    # EdgeOnly p95 at load 1 = 5.8 s (completions 2 and 6) > 1.5 x 2 s: never acceptable
    assert "EdgeOnly: acceptable up to load L* = –" in report
    assert "| 1.0 | 2.0 | 0.0 | 100.0 |" in report        # Fog absorbed all of EdgeOnly's overflow at load 1
    assert (out / "p95_completion_s_vs_load.png").stat().st_size > 1000
    # regenerating from the same files gives the same tables (no hidden state)
    before = (out / "summary.csv").read_bytes()
    analyze.main([str(block), "--no-plots"])
    assert (out / "summary.csv").read_bytes() == before
