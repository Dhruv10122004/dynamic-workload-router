"""Controller logic (plan §6): registration, heartbeat handling, submission, scheduling loop.

M1 scope: placement must name a worker (`_pinned_decision`). M3 replaces that with the
pure scheduler/ package; the transaction shape (one placement = one tx with its
decision record) already matches plan §6.3.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass

from controller.db import Database, add_event, insert_job_event, transition_job
from controller.settings import Settings
from shared.schemas import (
    SCHEMA_VERSION, Assignment, Heartbeat, HeartbeatReply, JobSnapshot, JobSubmit, JobValidationError,
    RegisterReply, RegisterRequest, validate_job,
)
from shared.states import COMMITTED, NON_TERMINAL, IllegalTransition, JobState, is_terminal

log = logging.getLogger("dwr.controller")

M1_POLICY = "pinned_m1"


class ServiceError(Exception):
    status = 400

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class Invalid(ServiceError):
    status = 422


@dataclass
class WorkerRuntime:
    """In-memory cache per worker; never persisted (plan §6.6)."""
    instance_id: str | None = None
    last_seq: int = -1
    last_seen_mono: float | None = None
    telemetry: dict | None = None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _in(states) -> str:
    return "(" + ",".join(f"'{s.value}'" for s in states) + ")"


class ControllerService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.rt: dict[str, WorkerRuntime] = {}
        self._unknown_logged: set[tuple[str, str]] = set()
        self._kick: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._kick = asyncio.Event()
        self._task = asyncio.create_task(self._scheduler_loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def kick(self) -> None:
        if self._kick is not None:
            self._kick.set()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), self.settings.scheduler_interval_s)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            try:
                await self.schedule_pass()
            except Exception:
                log.exception("scheduler pass failed")

    # ------------------------------------------------------------ freshness
    def is_fresh(self, worker_id: str) -> bool:
        rt = self.rt.get(worker_id)
        return bool(rt and rt.last_seen_mono is not None
                    and time.monotonic() - rt.last_seen_mono <= self.settings.stale_s)

    def heartbeat_age_s(self, worker_id: str) -> float | None:
        rt = self.rt.get(worker_id)
        return None if rt is None or rt.last_seen_mono is None else time.monotonic() - rt.last_seen_mono

    # ------------------------------------------------------------ registration
    async def register(self, req: RegisterRequest, token: str) -> RegisterReply:
        if req.schema_version != SCHEMA_VERSION:
            raise Conflict(f"incompatible schema_version {req.schema_version}; controller speaks {SCHEMA_VERSION}")
        now = time.time()
        async with self.db.lock:
            with self.db.tx() as c:
                prev = c.execute("SELECT agent_instance_id FROM workers WHERE worker_id=?",
                                 (req.worker_id,)).fetchone()
                c.execute(
                    """INSERT INTO workers (worker_id, tier, hostname, os, os_version, python, cpu_logical,
                         mem_total_mb, slots_json, mem_budget_mb, mem_floor_mb, supported_types_json,
                         capabilities_json, agent_version, agent_instance_id, token_hash, registered_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(worker_id) DO UPDATE SET tier=excluded.tier, hostname=excluded.hostname,
                         os=excluded.os, os_version=excluded.os_version, python=excluded.python,
                         cpu_logical=excluded.cpu_logical, mem_total_mb=excluded.mem_total_mb,
                         slots_json=excluded.slots_json, mem_budget_mb=excluded.mem_budget_mb,
                         mem_floor_mb=excluded.mem_floor_mb, supported_types_json=excluded.supported_types_json,
                         capabilities_json=excluded.capabilities_json, agent_version=excluded.agent_version,
                         agent_instance_id=excluded.agent_instance_id, token_hash=excluded.token_hash""",
                    (req.worker_id, req.tier, req.hostname, req.os, req.os_version, req.python,
                     req.cpu_logical, req.mem_total_mb, json.dumps([s.model_dump() for s in req.slots]),
                     req.mem_budget_mb, req.mem_floor_mb, json.dumps(sorted(req.supported_types)),
                     req.capabilities.model_dump_json(), req.agent_version, req.agent_instance_id,
                     token_hash(token), now),
                )
                add_event(c, "worker_registered", worker_id=req.worker_id, ts=now,
                          hostname=req.hostname, os=req.os, instance=req.agent_instance_id,
                          previous_instance=prev["agent_instance_id"] if prev else None)
        self.kick()
        return RegisterReply(accepted=True, heartbeat_interval_s=self.settings.heartbeat_interval_s,
                             stale_after_s=self.settings.stale_s)

    # ------------------------------------------------------------ heartbeat
    async def heartbeat(self, worker_id: str, hb: Heartbeat) -> HeartbeatReply:
        t0 = time.perf_counter()
        if hb.schema_version != SCHEMA_VERSION:
            raise Conflict(f"incompatible schema_version {hb.schema_version}; controller speaks {SCHEMA_VERSION}")
        now = time.time()
        async with self.db.lock:
            worker = self.db.one("SELECT * FROM workers WHERE worker_id=?", (worker_id,))
            if worker is None:
                raise NotFound(f"worker {worker_id!r} is not registered")
            rt = self.rt.setdefault(worker_id, WorkerRuntime())
            was_fresh = self.is_fresh(worker_id)
            with self.db.tx() as c:
                apply = True
                if rt.instance_id != hb.agent_instance_id:
                    if rt.instance_id is not None:
                        add_event(c, "agent_restarted", worker_id=worker_id, ts=now,
                                  old_instance=rt.instance_id, new_instance=hb.agent_instance_id)
                    rt.instance_id = hb.agent_instance_id
                elif hb.seq <= rt.last_seq:
                    apply = False  # duplicate or reordered: reply only (replies are level-triggered)
                if apply:
                    rt.last_seq = hb.seq
                rt.last_seen_mono = time.monotonic()
                rt.telemetry = hb.telemetry.model_dump()
                c.execute("UPDATE workers SET last_heartbeat_ts=?, telemetry_json=? WHERE worker_id=?",
                          (now, json.dumps(rt.telemetry), worker_id))
                changed = self._apply_snapshot(c, worker_id, hb.jobs, now) if apply else False
            reply = self._build_reply(worker_id, worker, hb)
        if changed or not was_fresh:
            self.kick()
        reply.server_processing_ms = (time.perf_counter() - t0) * 1000
        return reply

    def _apply_snapshot(self, c: sqlite3.Connection, worker_id: str, jobs: list[JobSnapshot],
                        now: float) -> bool:
        """Apply a worker snapshot. Returns True if any job reached a terminal state."""
        any_terminal = False
        for s in jobs:
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (s.job_id,)).fetchone()
            if row is None or row["worker_id"] != worker_id:
                if (worker_id, s.job_id) not in self._unknown_logged:
                    self._unknown_logged.add((worker_id, s.job_id))
                    add_event(c, "unknown_job_in_snapshot", worker_id=worker_id, job_id=s.job_id, ts=now,
                              state=s.state)
                continue
            fields = _snapshot_fields(s)
            if not row["acked"]:
                fields.update(acked=1, ack_ts=now)
            if is_terminal(row["state"]):
                if s.state != row["state"] or (fields.get("result_json") or row["result_json"]) != row["result_json"]:
                    if row["late_result_json"] is None:
                        c.execute("UPDATE jobs SET late_result_json=? WHERE job_id=?",
                                  (s.model_dump_json(), s.job_id))
                        add_event(c, "late_result", worker_id=worker_id, job_id=s.job_id, ts=now,
                                  controller_state=row["state"], reported_state=s.state)
                continue
            if s.state == row["state"]:
                if fields:
                    c.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE job_id=?",
                              (*fields.values(), s.job_id))
                continue
            terminal = is_terminal(s.state)
            if row["start_ts"] is None and (s.state == JobState.RUNNING.value or (terminal and s.pid)):
                fields["start_ts"] = now
            if terminal:
                fields["end_ts"] = now
            try:
                transition_job(c, s.job_id, s.state, "snapshot", ts=now, fail_reason=s.fail_reason,
                               kill_reason=s.kill_reason, **fields)
            except IllegalTransition as e:
                add_event(c, "illegal_snapshot_transition", worker_id=worker_id, job_id=s.job_id, ts=now,
                          error=str(e))
                continue
            any_terminal |= terminal
        return any_terminal

    def _build_reply(self, worker_id: str, worker: sqlite3.Row, hb: Heartbeat) -> HeartbeatReply:
        listed = {j.job_id for j in hb.jobs}
        assignments = [
            Assignment(job_id=r["job_id"], type=r["type"], params=json.loads(r["params_json"]),
                       input_bytes=r["input_bytes"], input_sha256=r["input_sha256"], input_url=None,
                       max_runtime_s=r["max_runtime_s"], timeout_s=r["timeout_s"], mem_mb=r["mem_mb"])
            for r in self.db.all("SELECT * FROM jobs WHERE worker_id=? AND state='ASSIGNED'"
                                 " ORDER BY assign_ts, rowid", (worker_id,))
            if r["job_id"] not in listed
        ]
        cancel = [r["job_id"] for r in self.db.all(
            f"SELECT job_id FROM jobs WHERE worker_id=? AND cancel_requested=1 AND state IN {_in(NON_TERMINAL)}",
            (worker_id,))]
        ack = []
        for j in hb.jobs:
            if not is_terminal(j.state):
                continue
            row = self.db.one("SELECT state, worker_id FROM jobs WHERE job_id=?", (j.job_id,))
            if row is None or row["worker_id"] != worker_id or is_terminal(row["state"]):
                ack.append(j.job_id)
        return HeartbeatReply(seq_ack=hb.seq, server_processing_ms=0.0, drain=bool(worker["draining"]),
                              assignments=assignments, cancel=cancel, ack_terminal=ack)

    # ------------------------------------------------------------ jobs
    async def submit(self, sub: JobSubmit) -> dict:
        try:
            v = validate_job(sub.type, sub.params, sub.timeout_s, test_mode=self.settings.test_mode)
        except JobValidationError as e:
            raise Invalid(e.errors) from None
        if sub.placement == "auto":
            raise Invalid([{"loc": ["placement"],
                            "msg": "placement=auto arrives with the scheduler in M3; name a worker_id"}])
        if self.db.one("SELECT 1 FROM workers WHERE worker_id=?", (sub.placement,)) is None:
            raise Invalid([{"loc": ["placement"], "msg": f"unknown worker {sub.placement!r}"}])
        job_id = uuid.uuid4().hex
        now = time.time()
        async with self.db.lock:
            with self.db.tx() as c:
                c.execute(
                    """INSERT INTO jobs (job_id, type, params_json, mem_mb, max_runtime_s, timeout_s, placement,
                         state, submit_ts, run_id, trace_index) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, v.type, json.dumps(v.params), v.mem_mb, v.max_runtime_s, v.timeout_s,
                     sub.placement, JobState.PENDING.value, now, sub.run_id, sub.trace_index),
                )
                insert_job_event(c, job_id, None, JobState.PENDING.value, "submit", None, now)
        self.kick()
        return {"job_id": job_id, "state": JobState.PENDING.value}

    def get_job(self, job_id: str) -> dict:
        row = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if row is None:
            raise NotFound(f"job {job_id!r} not found")
        job = job_to_json(row)
        job["transitions"] = [dict(r) for r in self.db.all(
            "SELECT ts, from_state, to_state, trigger, reason FROM job_events WHERE job_id=? ORDER BY id",
            (job_id,))]
        d = self.db.one("SELECT * FROM decisions WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
        job["decision"] = None if d is None else decision_to_json(d)
        return job

    def list_jobs(self, *, state: str | None, worker: str | None, run_id: str | None,
                  limit: int, after: str | None) -> list[dict]:
        where, params = [], []
        for col, val in (("state", state), ("worker_id", worker), ("run_id", run_id)):
            if val:
                where.append(f"{col}=?")
                params.append(val)
        if after:
            where.append("rowid > (SELECT rowid FROM jobs WHERE job_id=?)")
            params.append(after)
        sql = "SELECT * FROM jobs" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY rowid LIMIT ?"
        return [job_to_json(r) for r in self.db.all(sql, (*params, limit))]

    # ------------------------------------------------------------ workers
    def list_workers(self) -> list[dict]:
        return [self.worker_to_json(r) for r in self.db.all("SELECT * FROM workers ORDER BY worker_id")]

    def get_worker(self, worker_id: str) -> dict:
        row = self.db.one("SELECT * FROM workers WHERE worker_id=?", (worker_id,))
        if row is None:
            raise NotFound(f"worker {worker_id!r} not found")
        return self.worker_to_json(row)

    def worker_to_json(self, r: sqlite3.Row) -> dict:
        used = self.db.one(
            f"SELECT COUNT(*) n, COALESCE(SUM(mem_mb),0) mem FROM jobs WHERE worker_id=? AND state IN {_in(COMMITTED)}",
            (r["worker_id"],))
        slots = json.loads(r["slots_json"])
        return {
            "worker_id": r["worker_id"], "tier": r["tier"], "hostname": r["hostname"], "os": r["os"],
            "os_version": r["os_version"], "python": r["python"], "cpu_logical": r["cpu_logical"],
            "mem_total_mb": r["mem_total_mb"], "slots": slots, "slots_total": len(slots),
            "slots_used": used["n"], "mem_budget_mb": r["mem_budget_mb"], "mem_committed_mb": used["mem"],
            "mem_floor_mb": r["mem_floor_mb"], "supported_types": json.loads(r["supported_types_json"]),
            "capabilities": json.loads(r["capabilities_json"] or "{}"), "agent_version": r["agent_version"],
            "registered_at": r["registered_at"], "last_heartbeat_ts": r["last_heartbeat_ts"],
            "heartbeat_age_s": self.heartbeat_age_s(r["worker_id"]), "fresh": self.is_fresh(r["worker_id"]),
            "draining": bool(r["draining"]), "telemetry": json.loads(r["telemetry_json"] or "null"),
        }

    # ------------------------------------------------------------ scheduling
    async def schedule_pass(self) -> int:
        """One FIFO pass over PENDING jobs; one transaction per placement. Returns placements made."""
        placed = 0
        async with self.db.lock:
            pending = self.db.all("SELECT job_id FROM jobs WHERE state='PENDING' ORDER BY submit_ts, rowid")
            for (job_id,) in pending:
                t0 = time.perf_counter()
                with self.db.tx() as c:
                    job = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                    if job is None or job["state"] != JobState.PENDING.value:
                        continue
                    worker, chosen, ineligible, structural = self._pinned_decision(c, job)
                    if chosen is None and not structural:
                        continue  # stays PENDING; no decision row for a no-op pass
                    now = time.time()
                    c.execute(
                        """INSERT INTO decisions (job_id, ts, policy, worker_id, eligible_json, ineligible_json,
                             structural_reject, decision_us) VALUES (?,?,?,?,?,?,?,?)""",
                        (job_id, now, M1_POLICY, chosen, json.dumps([chosen] if chosen else []),
                         json.dumps(ineligible), int(structural), (time.perf_counter() - t0) * 1e6),
                    )
                    if chosen:
                        transition_job(c, job_id, JobState.ASSIGNED, "scheduler", ts=now, worker_id=chosen,
                                       assign_ts=now, hostname=worker["hostname"])
                        placed += 1
                    else:
                        transition_job(c, job_id, JobState.REJECTED, "scheduler", ts=now)
        return placed

    def _pinned_decision(self, c: sqlite3.Connection, job: sqlite3.Row):
        """M1 placement: only the named worker, with the plan §7.1 eligibility checks inline.
        Returns (worker_row, chosen_id | None, ineligible {id: reason}, structural_reject)."""
        wid = job["placement"]
        w = c.execute("SELECT * FROM workers WHERE worker_id=?", (wid,)).fetchone()
        if w is None:
            return None, None, {wid: "unknown_worker"}, True
        if job["type"] not in json.loads(w["supported_types_json"]):
            return w, None, {wid: "unsupported_type"}, True
        if job["mem_mb"] > w["mem_budget_mb"]:
            return w, None, {wid: "mem_budget"}, True
        used = c.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(mem_mb),0) mem FROM jobs WHERE worker_id=? AND state IN {_in(COMMITTED)}",
            (wid,)).fetchone()
        telemetry = (self.rt.get(wid) or WorkerRuntime()).telemetry or {}
        reason = None
        if not self.is_fresh(wid):
            reason = "stale"
        elif w["draining"]:
            reason = "draining"
        elif used["n"] >= len(json.loads(w["slots_json"])):
            reason = "no_free_slot"
        elif used["mem"] + job["mem_mb"] > w["mem_budget_mb"]:
            reason = "mem_budget"
        elif telemetry.get("mem_available_mb", 0) - job["mem_mb"] < w["mem_floor_mb"]:
            reason = "mem_floor"
        return (w, None, {wid: reason}, False) if reason else (w, wid, {}, False)


def _snapshot_fields(s: JobSnapshot) -> dict:
    f = {k: getattr(s, k) for k in ("pid", "pid_create_time", "transfer_bytes", "transfer_s", "spawn_s",
                                    "exit_code", "error", "kill_latency_ms") if getattr(s, k) is not None}
    if s.termination_verified is not None:
        f["termination_verified"] = int(s.termination_verified)
    if s.result is not None:
        f["result_json"] = json.dumps(s.result, sort_keys=True)
        for k in ("exec_s", "cpu_s", "peak_rss_mb"):
            if isinstance(s.result.get(k), (int, float)):
                f[k] = s.result[k]
    return f


def job_to_json(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.pop("params_json"))
    d["result"] = json.loads(d.pop("result_json") or "null")
    d["late_result"] = json.loads(d.pop("late_result_json") or "null")
    for k in ("acked", "cancel_requested", "overflow"):
        d[k] = bool(d[k])
    if d["termination_verified"] is not None:
        d["termination_verified"] = bool(d["termination_verified"])
    if d["submit_ts"] is not None and d["end_ts"] is not None:
        d["completion_s"] = d["end_ts"] - d["submit_ts"]
    return d


def decision_to_json(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["eligible"] = json.loads(d.pop("eligible_json"))
    d["ineligible"] = json.loads(d.pop("ineligible_json"))
    d["structural_reject"] = bool(d["structural_reject"])
    return d
