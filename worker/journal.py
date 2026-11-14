"""Worker journal (plan §5.3): SQLite WAL, synchronous=FULL, write-ahead order.

Thread-safe: the heartbeat thread and the supervisor thread share one connection
behind a lock. Every method is one committed transaction.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from shared.states import TERMINAL, JobState, can_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    state TEXT NOT NULL,
    slot INTEGER,
    pid INTEGER,
    pid_create_time REAL,
    job_dir TEXT,
    transfer_bytes INTEGER,
    transfer_s REAL,
    spawn_s REAL,
    exit_code INTEGER,
    result_json TEXT,
    error TEXT,
    fail_reason TEXT,
    kill_reason TEXT,
    termination_verified INTEGER,
    kill_latency_ms REAL,
    reported_terminal INTEGER DEFAULT 0,
    accepted_at REAL NOT NULL,
    reported_at REAL
);
"""

PRUNE_AFTER_S = 24 * 3600
_TERMINAL_VALUES = tuple(s.value for s in TERMINAL)


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- step 1
    def accept(self, job_id: str, spec: dict, *, cancelled: bool = False,
               invalid_error: str | None = None) -> bool:
        """Insert a new assignment. Returns False (and changes nothing) for a known job_id."""
        if invalid_error is not None:
            state, fail_reason = JobState.FAILED, "worker_validation"
        else:
            state, fail_reason = (JobState.CANCELLED if cancelled else JobState.ACCEPTED), None
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO jobs (job_id, spec_json, state, fail_reason, error, accepted_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, json.dumps(spec), state.value, fail_reason, invalid_error, time.time()),
            )
            return cur.rowcount == 1

    # -- step 2
    def mark_running(self, job_id: str, *, slot: int, pid: int, pid_create_time: float, job_dir: str,
                     spawn_s: float, transfer_bytes: int | None = None, transfer_s: float | None = None) -> None:
        self._update(job_id, JobState.RUNNING, slot=slot, pid=pid, pid_create_time=pid_create_time,
                     job_dir=job_dir, spawn_s=spawn_s, transfer_bytes=transfer_bytes, transfer_s=transfer_s)

    # -- step 3
    def mark_terminal(self, job_id: str, state: JobState, **fields) -> None:
        assert state in TERMINAL, state
        if "result" in fields:
            r = fields.pop("result")
            fields["result_json"] = None if r is None else json.dumps(r)
        self._update(job_id, state, **fields)

    # -- step 4
    def ack_terminal(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        with self._lock:
            self._conn.executemany(
                f"UPDATE jobs SET reported_terminal=1, reported_at=? WHERE job_id=?"
                f" AND state IN ({','.join('?' * len(_TERMINAL_VALUES))})",
                [(time.time(), j, *_TERMINAL_VALUES) for j in job_ids],
            )

    def prune(self, now: float | None = None) -> int:
        cutoff = (now or time.time()) - PRUNE_AFTER_S
        with self._lock:
            return self._conn.execute(
                "DELETE FROM jobs WHERE reported_terminal=1 AND reported_at < ?", (cutoff,)
            ).rowcount

    # -- reads
    def get(self, job_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def accepted_fifo(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM jobs WHERE state='ACCEPTED' ORDER BY accepted_at, rowid"
            ).fetchall()

    def snapshot_rows(self) -> list[sqlite3.Row]:
        """Every non-terminal job plus terminal jobs the controller has not acknowledged."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM jobs WHERE reported_terminal=0 ORDER BY accepted_at, rowid"
            ).fetchall()

    def _update(self, job_id: str, state: JobState, **fields) -> None:
        with self._lock:
            row = self._conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if not can_transition(row["state"], state):
                raise ValueError(f"journal: {job_id} {row['state']} -> {state.value} not allowed")
            cols = ["state=?"] + [f"{k}=?" for k in fields]
            self._conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE job_id=?",
                               (state.value, *fields.values(), job_id))


def row_to_snapshot(row: sqlite3.Row) -> dict:
    tv = row["termination_verified"]
    return {
        "job_id": row["job_id"], "state": row["state"], "slot": row["slot"], "pid": row["pid"],
        "pid_create_time": row["pid_create_time"], "transfer_bytes": row["transfer_bytes"],
        "transfer_s": row["transfer_s"], "spawn_s": row["spawn_s"], "exit_code": row["exit_code"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"], "fail_reason": row["fail_reason"], "kill_reason": row["kill_reason"],
        "termination_verified": None if tv is None else bool(tv), "kill_latency_ms": row["kill_latency_ms"],
    }
