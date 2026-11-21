"""Overbooking audit (AC-3.3): replay job_events and check that no worker ever held more
committed jobs than its slots, or more committed memory than its budget.

A job holds a reservation from ASSIGNED until it reaches a terminal state (plan §6.3;
UNKNOWN keeps it). job_events ids follow commit order under the single writer, so the
replay sees every instant the controller saw.

    python -m experiments.audit [--db var/controller/dwr.db] [--run-id ID]
Exit code 0 = no overbooking, 1 = violations found.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shared.states import COMMITTED

_COMMITTED = {s.value for s in COMMITTED}


@dataclass
class WorkerPeak:
    slots_total: int
    mem_budget_mb: int
    peak_slots: int = 0
    peak_mem_mb: int = 0
    violations: list[str] = field(default_factory=list)


def audit(db_path: Path, run_id: str | None = None) -> dict[str, WorkerPeak]:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    peaks = {w["worker_id"]: WorkerPeak(len(json.loads(w["slots_json"])), w["mem_budget_mb"])
             for w in c.execute("SELECT worker_id, slots_json, mem_budget_mb FROM workers")}
    jobs = {j["job_id"]: j for j in c.execute("SELECT job_id, worker_id, mem_mb, run_id FROM jobs")}
    held: dict[str, set[str]] = {w: set() for w in peaks}
    for e in c.execute("SELECT id, job_id, ts, to_state FROM job_events ORDER BY id"):
        job = jobs.get(e["job_id"])
        if job is None or job["worker_id"] not in peaks:
            continue
        if run_id is not None and job["run_id"] != run_id:
            continue
        mine = held[job["worker_id"]]
        (mine.add if e["to_state"] in _COMMITTED else mine.discard)(e["job_id"])
        p = peaks[job["worker_id"]]
        n, mem = len(mine), sum(jobs[j]["mem_mb"] for j in mine)
        p.peak_slots, p.peak_mem_mb = max(p.peak_slots, n), max(p.peak_mem_mb, mem)
        if n > p.slots_total or mem > p.mem_budget_mb:
            p.violations.append(f"event {e['id']} ts={e['ts']:.3f}: {n}/{p.slots_total} slots, "
                                f"{mem}/{p.mem_budget_mb} MB")
    c.close()
    return peaks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=Path("var/controller/dwr.db"))
    ap.add_argument("--run-id")
    a = ap.parse_args(argv)
    peaks = audit(a.db, a.run_id)
    bad = 0
    for wid, p in sorted(peaks.items()):
        status = "OK" if not p.violations else f"OVERBOOKED x{len(p.violations)}"
        print(f"{wid}: peak {p.peak_slots}/{p.slots_total} slots, {p.peak_mem_mb}/{p.mem_budget_mb} MB  {status}")
        for v in p.violations[:10]:
            print("   ", v)
        bad += len(p.violations)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
