"""Experiment runs (plan §8.2, §9, §12.7): CSV export of one run's tables. Read-only helpers.

A run's jobs carry its run_id; telemetry samples and events are selected by the run's
[start_ts, end_ts] window (they belong to workers, not to runs).
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3

EXPORT_TABLES = ("jobs", "decisions", "telemetry", "events")

JOB_COLUMNS = [
    "job_id", "run_id", "trace_index", "type", "params_json", "mem_mb", "timeout_s", "placement",
    "worker_id", "tier", "hostname", "pid", "state", "fail_reason", "kill_reason", "overflow",
    "submit_ts", "assign_ts", "ack_ts", "start_ts", "end_ts", "queue_wait_s", "completion_s",
    "input_bytes", "transfer_bytes", "transfer_s", "spawn_s", "exec_s", "cpu_s", "peak_rss_mb",
    "exit_code", "termination_verified", "kill_latency_ms", "cancel_requested", "error",
]


def run_to_json(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["config"] = json.loads(d.pop("config_json") or "null")
    return d


def _window(run: sqlite3.Row, now: float) -> tuple[float, float]:
    return run["start_ts"], run["end_ts"] if run["end_ts"] is not None else now


def export_rows(c: sqlite3.Connection, run: sqlite3.Row, table: str, now: float) -> tuple[list[str], list[list]]:
    run_id = run["run_id"]
    t0, t1 = _window(run, now)
    if table == "jobs":
        rows = c.execute(
            """SELECT j.*, w.tier,
                      CASE WHEN j.assign_ts IS NOT NULL THEN j.assign_ts - j.submit_ts END AS queue_wait_s,
                      CASE WHEN j.end_ts IS NOT NULL THEN j.end_ts - j.submit_ts END AS completion_s
               FROM jobs j LEFT JOIN workers w ON w.worker_id = j.worker_id
               WHERE j.run_id = ? ORDER BY j.trace_index, j.rowid""", (run_id,)).fetchall()
        return JOB_COLUMNS, [[r[k] for k in JOB_COLUMNS] for r in rows]
    if table == "decisions":
        cols = ["id", "job_id", "trace_index", "ts", "policy", "worker_id", "eligible_json", "ineligible_json",
                "structural_reject", "decision_us"]
        rows = c.execute(
            """SELECT d.*, j.trace_index FROM decisions d JOIN jobs j ON j.job_id = d.job_id
               WHERE j.run_id = ? ORDER BY d.id""", (run_id,)).fetchall()
        return cols, [[r[k] for k in cols] for r in rows]
    if table == "telemetry":
        cols = ["id", "worker_id", "tier", "ts", "cpu_pct", "foreign_cpu_pct", "mem_available_mb", "mem_total_mb",
                "slots_used", "slots_total", "host_busy"]
        rows = c.execute(
            """SELECT t.*, w.tier, w.mem_total_mb, w.slots_json FROM telemetry_samples t
               LEFT JOIN workers w ON w.worker_id = t.worker_id
               WHERE t.ts BETWEEN ? AND ? ORDER BY t.id""", (t0, t1)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["slots_total"] = len(json.loads(d["slots_json"])) if d["slots_json"] else None
            out.append([d[k] for k in cols])
        return cols, out
    if table == "events":
        cols = ["id", "ts", "kind", "worker_id", "job_id", "details_json"]
        rows = c.execute("SELECT * FROM events WHERE ts BETWEEN ? AND ? ORDER BY id", (t0, t1)).fetchall()
        return cols, [[r[k] for k in cols] for r in rows]
    raise ValueError(f"unknown table {table!r}; one of {EXPORT_TABLES}")


def to_csv(cols: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    w.writerows(rows)
    return buf.getvalue()
