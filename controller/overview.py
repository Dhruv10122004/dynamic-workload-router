"""Overview numbers for the dashboard and the run reports (plan §10, §12.4). Read-only.

Everything is derived from SQLite: jobs for counts and placement, job_events for the
backlog over time. Tier names come from the workers table, never from constants.
"""
from __future__ import annotations

import sqlite3
from collections import Counter

from scheduler.views import TIER_RANK


def backlog_stats(events: list[sqlite3.Row], now: float) -> tuple[int, float]:
    """(peak_backlog, overflow_time_s) by replaying PENDING entries/exits in commit order.
    overflow_time_s = total time the PENDING queue was non-empty (plan §12.4)."""
    n = peak = 0
    busy_since: float | None = None
    total = 0.0
    for e in events:
        if e["to_state"] == "PENDING":
            n += 1
        elif e["from_state"] == "PENDING":
            n -= 1
        else:
            continue
        peak = max(peak, n)
        if n > 0 and busy_since is None:
            busy_since = e["ts"]
        elif n == 0 and busy_since is not None:
            total += e["ts"] - busy_since
            busy_since = None
    if busy_since is not None:
        total += max(0.0, now - busy_since)
    return peak, total


def compute(c: sqlite3.Connection, now: float, run_id: str | None = None) -> dict:
    where, params = ("WHERE j.run_id = ?", (run_id,)) if run_id else ("", ())
    jobs = c.execute(
        f"""SELECT j.state, j.worker_id, j.cpu_s, j.exec_s, j.overflow, w.tier
            FROM jobs j LEFT JOIN workers w ON w.worker_id = j.worker_id {where}""", params).fetchall()
    by_state = Counter(j["state"] for j in jobs)
    tiers: dict[str, dict] = {}
    for j in jobs:
        if j["tier"] is None:
            continue
        t = tiers.setdefault(j["tier"], {"placed": 0, "completed": 0, "cpu_s": 0.0})
        t["placed"] += 1
        if j["state"] == "SUCCEEDED":
            t["completed"] += 1
            t["cpu_s"] += j["cpu_s"] or 0.0
    done = sum(t["completed"] for t in tiers.values())
    cpu = sum(t["cpu_s"] for t in tiers.values())
    # "Local offload": work done away from the first tier (Edge) on another local tier (plan §10).
    # The first tier is the lowest-ranked *registered* one, so a FogOnly run still counts as offload.
    registered = [r["tier"] for r in c.execute("SELECT DISTINCT tier FROM workers")]
    first = min(registered, key=lambda t: TIER_RANK.get(t, 99), default=None)
    off_n = sum(t["completed"] for k, t in tiers.items() if k != first)
    off_cpu = sum(t["cpu_s"] for k, t in tiers.items() if k != first)
    overflow = [j for j in jobs if j["overflow"]]

    ev_sql = """SELECT e.ts, e.from_state, e.to_state FROM job_events e
                WHERE (e.to_state = 'PENDING' OR e.from_state = 'PENDING')"""
    if run_id:
        ev_sql += " AND e.job_id IN (SELECT job_id FROM jobs WHERE run_id = ?)"
    peak, overflow_time = backlog_stats(c.execute(ev_sql + " ORDER BY e.id", params).fetchall(), now)
    return {
        "run_id": run_id,
        "total_jobs": len(jobs),
        "by_state": dict(sorted(by_state.items())),
        "tiers": {k: {**v, "cpu_s": round(v["cpu_s"], 3)} for k, v in sorted(tiers.items(),
                                                                          key=lambda kv: TIER_RANK.get(kv[0], 99))},
        "first_tier": first,
        "completed": done,
        "local_offload_pct_by_count": round(100 * off_n / done, 1) if done else None,
        "local_offload_pct_by_cpu_s": round(100 * off_cpu / cpu, 1) if cpu else None,
        "backlog": by_state.get("PENDING", 0),
        "peak_backlog": peak,
        "overflow_jobs": len(overflow),
        "overflow_demand_s": round(sum(j["exec_s"] or 0.0 for j in overflow), 3),
        "overflow_time_s": round(overflow_time, 3),
    }
