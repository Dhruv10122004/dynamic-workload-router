"""Controller SQLite (plan §6.3, §9): schema, WAL, single-writer lock, transactions.

Rules:
- Every write runs under `async with db.lock:` and inside `with db.tx() as c:`.
- Every job state change goes through `transition_job()`, which validates it with
  shared.states.transition() and writes the job_events row in the same transaction.
- The connection is only used from the event-loop thread; nothing here awaits.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from shared.states import JobState, transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL,
    hostname TEXT, os TEXT, os_version TEXT, python TEXT,
    cpu_logical INTEGER, mem_total_mb INTEGER,
    slots_json TEXT NOT NULL,
    mem_budget_mb INTEGER NOT NULL,
    mem_floor_mb INTEGER NOT NULL,
    supported_types_json TEXT NOT NULL,
    capabilities_json TEXT,
    agent_version TEXT,
    agent_instance_id TEXT,
    token_hash TEXT,
    registered_at REAL NOT NULL,
    last_heartbeat_ts REAL,
    telemetry_json TEXT,
    draining INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    params_json TEXT NOT NULL,
    mem_mb INTEGER NOT NULL,
    max_runtime_s REAL NOT NULL,
    timeout_s REAL,
    placement TEXT NOT NULL,
    state TEXT NOT NULL,
    fail_reason TEXT,
    kill_reason TEXT,
    worker_id TEXT,
    acked INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    submit_ts REAL NOT NULL,
    assign_ts REAL, ack_ts REAL, start_ts REAL, end_ts REAL,
    input_bytes INTEGER NOT NULL DEFAULT 0,
    input_sha256 TEXT,
    transfer_bytes INTEGER, transfer_s REAL, spawn_s REAL,
    exec_s REAL, cpu_s REAL, peak_rss_mb REAL,
    exit_code INTEGER,
    pid INTEGER, pid_create_time REAL, hostname TEXT,
    result_json TEXT,
    error TEXT,
    termination_verified INTEGER,
    kill_latency_ms REAL,
    overflow INTEGER NOT NULL DEFAULT 0,
    run_id TEXT, trace_index INTEGER,
    late_result_json TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_worker_state ON jobs(worker_id, state);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    trigger TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    policy TEXT NOT NULL,
    worker_id TEXT,
    eligible_json TEXT NOT NULL,
    ineligible_json TEXT NOT NULL,
    structural_reject INTEGER NOT NULL DEFAULT 0,
    decision_us REAL
);
CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job_id);
CREATE TABLE IF NOT EXISTS telemetry_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    ts REAL NOT NULL,
    cpu_pct REAL, foreign_cpu_pct REAL, mem_available_mb INTEGER,
    slots_used INTEGER, host_busy INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT, policy TEXT, trace_hash TEXT, config_json TEXT, git_commit TEXT,
    start_ts REAL, end_ts REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    worker_id TEXT,
    job_id TEXT,
    details_json TEXT
);
"""

# Columns a caller may set alongside a transition.
JOB_FIELDS = frozenset({
    "worker_id", "acked", "assign_ts", "ack_ts", "start_ts", "end_ts", "hostname",
    "transfer_bytes", "transfer_s", "spawn_s", "exec_s", "cpu_s", "peak_rss_mb", "exit_code",
    "pid", "pid_create_time", "result_json", "error", "termination_verified", "kill_latency_ms",
    "overflow", "late_result_json",
})


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Opened on the main thread, used on the event-loop thread (one at a time).
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.lock = asyncio.Lock()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()


def add_event(c: sqlite3.Connection, kind: str, *, worker_id: str | None = None,
              job_id: str | None = None, ts: float | None = None, **details) -> None:
    c.execute(
        "INSERT INTO events (ts, kind, worker_id, job_id, details_json) VALUES (?, ?, ?, ?, ?)",
        (ts or time.time(), kind, worker_id, job_id, json.dumps(details) if details else None),
    )


def insert_job_event(c: sqlite3.Connection, job_id: str, src: str | None, dst: str, trigger: str,
                     reason: str | None, ts: float) -> None:
    c.execute(
        "INSERT INTO job_events (job_id, ts, from_state, to_state, trigger, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, ts, src, dst, trigger, reason),
    )


def transition_job(c: sqlite3.Connection, job_id: str, dst: JobState | str, trigger: str, *,
                   ts: float, fail_reason: str | None = None, kill_reason: str | None = None,
                   **fields) -> JobState:
    """Validate and persist one job state change plus its job_events row. Must run inside tx()."""
    unknown = set(fields) - JOB_FIELDS
    if unknown:
        raise ValueError(f"not settable with a transition: {sorted(unknown)}")
    row = c.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    src = row["state"]
    new = transition(src, dst, fail_reason=fail_reason, kill_reason=kill_reason)
    cols = {"state": new.value, "fail_reason": fail_reason, "kill_reason": kill_reason, **fields}
    c.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in cols)} WHERE job_id=?",
              (*cols.values(), job_id))
    insert_job_event(c, job_id, src, new.value, trigger, fail_reason or kill_reason, ts)
    return new
