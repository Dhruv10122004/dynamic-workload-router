"""Controller logic (plan §6): registration, heartbeat handling, submission, scheduling loop.

Placement (plan §6.3, §6.4): one pass over PENDING jobs (aged first, then FIFO with skip);
each placement is one transaction: read committed jobs -> scheduler decide() -> decision
row -> transition. Host-busy state (plan §7.2) is tracked per worker from heartbeats.

Reliability (plan §6.5, §6.6): a 1 s maintenance sweep marks workers stale (their committed
jobs -> UNKNOWN), fails UNKNOWN jobs after LOST_TIMEOUT_S and PENDING jobs after
queue_timeout_s. A stale worker's next applied snapshot reconciles its UNKNOWN jobs.
After a controller restart every worker counts as unheard-of until its first heartbeat.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable

from controller import overview, runs
from controller.blobs import BlobStore
from controller.db import Database, add_event, insert_job_event, transition_job
from controller.settings import Settings
from shared.schemas import (
    SCHEMA_VERSION, Assignment, Heartbeat, HeartbeatReply, JobSnapshot, JobSubmit, JobValidationError,
    RegisterReply, RegisterRequest, validate_job,
)
from scheduler import HostBusy, JobView, WorkerView, make_policy
from scheduler.eligibility import could_ever_run
from scheduler.policies import POLICIES
from shared.states import COMMITTED, NON_TERMINAL, IllegalTransition, JobState, is_terminal

log = logging.getLogger("dwr.controller")


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
    host_busy: HostBusy = field(default_factory=HostBusy)
    stale: bool = False   # marked by the sweep; cleared by the next heartbeat
    job_stats: dict = field(default_factory=dict)   # job_id -> live cpu_pct/rss_mb (cache only, D-028)


@dataclass(frozen=True)
class SchedulerConfig:
    """Active policy and host-busy settings (plan §7.3, GET/PUT /scheduler; DECISIONS D-016)."""
    policy: str
    host_busy_enabled: bool
    host_busy_high: float
    host_busy_low: float
    aging_s: float
    queue_timeout_s: float = 300.0

    def validated(self) -> "SchedulerConfig":
        errors = []
        if self.policy not in POLICIES:
            errors.append({"loc": ["policy"], "msg": f"unknown policy; choose one of {sorted(POLICIES)}"})
        if not 0 <= self.host_busy_low < self.host_busy_high <= 100:
            errors.append({"loc": ["host_busy_low", "host_busy_high"], "msg": "need 0 <= low < high <= 100"})
        if self.aging_s <= 0:
            errors.append({"loc": ["aging_s"], "msg": "must be > 0"})
        if self.queue_timeout_s <= 0:
            errors.append({"loc": ["queue_timeout_s"], "msg": "must be > 0"})
        if errors:
            raise Invalid(errors)
        return self


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _in(states) -> str:
    return "(" + ",".join(f"'{s.value}'" for s in states) + ")"


class ControllerService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.rt: dict[str, WorkerRuntime] = {}
        self.blobs = BlobStore(settings.data_dir / "blobs")
        self._unknown_logged: set[tuple[str, str]] = set()
        self._kick: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self.started_mono = time.monotonic()
        self.sched = self._load_scheduler_config()
        self.policy = make_policy(self.sched.policy, host_busy_enabled=self.sched.host_busy_enabled)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._kick = asyncio.Event()
        self.started_mono = time.monotonic()
        self._tasks = [asyncio.create_task(self._scheduler_loop(), name="scheduler"),
                       asyncio.create_task(self._telemetry_loop(), name="telemetry"),
                       asyncio.create_task(self._maintenance_loop(), name="maintenance")]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
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

    async def _telemetry_loop(self) -> None:
        """One telemetry_samples row per fresh worker every telemetry_sample_s (plan §6.5)."""
        while True:
            await asyncio.sleep(self.settings.telemetry_sample_s)
            try:
                await self.sample_telemetry()
            except Exception:
                log.exception("telemetry sampling failed")

    async def sample_telemetry(self) -> None:
        now = time.time()
        async with self.db.lock:
            used = self._committed_usage()
            with self.db.tx() as c:
                for w in c.execute("SELECT worker_id FROM workers").fetchall():
                    rt = self.rt.get(w["worker_id"])
                    if rt is None or rt.telemetry is None or not self.is_fresh(w["worker_id"]):
                        continue
                    tel = rt.telemetry
                    c.execute("""INSERT INTO telemetry_samples (worker_id, ts, cpu_pct, foreign_cpu_pct,
                                   mem_available_mb, slots_used, host_busy) VALUES (?,?,?,?,?,?,?)""",
                              (w["worker_id"], now, tel.get("cpu_pct"), tel.get("foreign_cpu_pct"),
                               tel.get("mem_available_mb"), used.get(w["worker_id"], (0, 0))[0],
                               int(rt.host_busy.busy)))

    # ------------------------------------------------------------ maintenance (plan §6.5)
    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.maintenance_interval_s)
            try:
                await self.maintenance()
            except Exception:
                log.exception("maintenance sweep failed")

    def _runtime(self, worker_id: str) -> WorkerRuntime:
        rt = self.rt.get(worker_id)
        if rt is None:
            rt = self.rt[worker_id] = WorkerRuntime(host_busy=self._new_tracker())
        return rt

    async def maintenance(self) -> int:
        """Staleness sweep, lost timeout, queue timeout. Returns the number of jobs that ended."""
        ended = 0
        now, mono = time.time(), time.monotonic()
        async with self.db.lock:
            with self.db.tx() as c:
                for w in c.execute("SELECT worker_id FROM workers").fetchall():
                    wid = w["worker_id"]
                    rt = self._runtime(wid)
                    age = mono - (rt.last_seen_mono if rt.last_seen_mono is not None else self.started_mono)
                    if age <= self.settings.stale_s:
                        continue
                    if not rt.stale:
                        rt.stale = True
                        was_busy = rt.host_busy.busy
                        rt.host_busy = self._new_tracker()  # old samples say nothing about the future
                        add_event(c, "worker_stale", worker_id=wid, ts=now, heartbeat_age_s=round(age, 2),
                                  host_busy_cleared=was_busy)
                        log.warning("worker %s is stale (no heartbeat for %.1f s)", wid, age)
                    for j in c.execute(
                            "SELECT job_id FROM jobs WHERE worker_id=? AND state IN ('ASSIGNED','ACCEPTED','RUNNING')",
                            (wid,)).fetchall():
                        transition_job(c, j["job_id"], JobState.UNKNOWN, "stale", ts=now)
                for j in c.execute(
                        """SELECT j.job_id, MAX(e.ts) since FROM jobs j JOIN job_events e
                           ON e.job_id = j.job_id AND e.to_state = 'UNKNOWN'
                           WHERE j.state = 'UNKNOWN' GROUP BY j.job_id""").fetchall():
                    if now - j["since"] > self.settings.lost_timeout_s:
                        transition_job(c, j["job_id"], JobState.FAILED, "lost_timeout", ts=now, fail_reason="lost",
                                       end_ts=now, error=f"UNKNOWN for more than {self.settings.lost_timeout_s:.0f} s")
                        ended += 1
                for j in c.execute("SELECT job_id FROM jobs WHERE state='PENDING' AND submit_ts < ?",
                                   (now - self.sched.queue_timeout_s,)).fetchall():
                    transition_job(c, j["job_id"], JobState.FAILED, "queue_timeout", ts=now,
                                   fail_reason="queue_timeout", end_ts=now,
                                   error=f"PENDING for more than {self.sched.queue_timeout_s:.0f} s")
                    ended += 1
        if ended:
            self.kick()
        return ended

    # ------------------------------------------------------------ cancel, drain (plan §8.2)
    async def cancel(self, job_id: str) -> dict:
        """PENDING -> CANCELLED now. Any other non-terminal job: cancel_requested, delivered in
        every reply until the worker reports the outcome (requested, not confirmed)."""
        now = time.time()
        async with self.db.lock:
            with self.db.tx() as c:
                row = c.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    raise NotFound(f"job {job_id!r} not found")
                if is_terminal(row["state"]):
                    raise Conflict(f"job {job_id!r} is already {row['state']}")
                if row["state"] == JobState.PENDING.value:
                    transition_job(c, job_id, JobState.CANCELLED, "cancel", ts=now, end_ts=now)
                else:
                    c.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,))
                add_event(c, "cancel_requested", job_id=job_id, ts=now, state=row["state"])
                state = c.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()["state"]
        return {"job_id": job_id, "cancel_requested": True, "state": state}

    async def set_draining(self, worker_id: str, draining: bool) -> dict:
        now = time.time()
        async with self.db.lock:
            with self.db.tx() as c:
                if c.execute("UPDATE workers SET draining=? WHERE worker_id=?",
                             (int(draining), worker_id)).rowcount == 0:
                    raise NotFound(f"worker {worker_id!r} not found")
                add_event(c, "worker_drain" if draining else "worker_enable", worker_id=worker_id, ts=now)
        self.kick()
        return self.get_worker(worker_id)

    # ------------------------------------------------------------ scheduler config
    def _load_scheduler_config(self) -> SchedulerConfig:
        s = self.settings
        default = SchedulerConfig(s.policy, s.host_busy_enabled, s.host_busy_high, s.host_busy_low, s.aging_s,
                                  s.queue_timeout_s)
        row = self.db.one("SELECT config_json FROM scheduler_config WHERE id=1")
        if row is None:
            return default
        try:
            return SchedulerConfig(**{**asdict(default), **json.loads(row["config_json"])}).validated()
        except (Invalid, TypeError, ValueError):
            log.warning("stored scheduler config is invalid; using the .env defaults")
            return default

    def _new_tracker(self, old: HostBusy | None = None) -> HostBusy:
        hb = HostBusy(high=self.sched.host_busy_high, low=self.sched.host_busy_low)
        if old is not None:
            hb.busy = old.busy
            hb.samples.extend(old.samples)
        return hb

    def get_scheduler(self) -> dict:
        return {**asdict(self.sched), "policies": sorted(POLICIES)}

    async def put_scheduler(self, changes: dict) -> dict:
        """Plan §7.3: refused with 409 while any job is non-terminal, so a run has one policy."""
        async with self.db.lock:
            busy = self.db.one(f"SELECT COUNT(*) n FROM jobs WHERE state IN {_in(NON_TERMINAL)}")["n"]
            if busy:
                raise Conflict(f"{busy} job(s) are not terminal; the scheduler can change only when idle")
            new = replace(self.sched, **changes).validated()
            now = time.time()
            with self.db.tx() as c:
                c.execute("""INSERT INTO scheduler_config (id, config_json, updated_ts) VALUES (1, ?, ?)
                             ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,
                               updated_ts=excluded.updated_ts""", (json.dumps(asdict(new)), now))
                add_event(c, "scheduler_changed", ts=now, old=asdict(self.sched), new=asdict(new))
            self.sched = new
            self.policy = make_policy(new.policy, host_busy_enabled=new.host_busy_enabled)
            for rt in self.rt.values():
                rt.host_busy = self._new_tracker(rt.host_busy)
        self.kick()
        return self.get_scheduler()

    def list_events(self, *, after: int, limit: int, kind: str | None = None,
                    newest_first: bool = False) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE id > ?", [after]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        out = []
        for r in self.db.all(sql + f" ORDER BY id {'DESC' if newest_first else 'ASC'} LIMIT ?", (*params, limit)):
            d = dict(r)
            d["details"] = json.loads(d.pop("details_json") or "null")
            out.append(d)
        return out

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
            rt = self._runtime(worker_id)
            was_fresh = self.is_fresh(worker_id)
            freed = False
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
                if rt.stale:
                    rt.stale = False
                    add_event(c, "worker_fresh", worker_id=worker_id, ts=now)
                    log.info("worker %s is fresh again", worker_id)
                rt.telemetry = hb.telemetry.model_dump()
                c.execute("UPDATE workers SET last_heartbeat_ts=?, telemetry_json=? WHERE worker_id=?",
                          (now, json.dumps(rt.telemetry), worker_id))
                if apply and rt.host_busy.add(hb.telemetry.foreign_cpu_pct):
                    busy = rt.host_busy.busy
                    freed = not busy
                    add_event(c, "host_busy" if busy else "host_free", worker_id=worker_id, ts=now,
                              mean_foreign_cpu_pct=round(rt.host_busy.mean, 1),
                              samples=len(rt.host_busy.samples), enforced=self.sched.host_busy_enabled)
                    log.info("worker %s is %s (foreign CPU mean %.1f%%)", worker_id,
                             "host_busy" if busy else "free again", rt.host_busy.mean)
                changed = self._apply_snapshot(c, worker_id, hb.jobs, now) if apply else False
                if apply:
                    rt.job_stats = {s.job_id: {"cpu_pct": s.cpu_pct, "rss_mb": s.rss_mb}
                                    for s in hb.jobs if s.state == "RUNNING" and s.rss_mb is not None}
            if changed or not was_fresh:
                # A slot just freed (or the worker is back): place now, so this very reply
                # carries the next assignment instead of the next heartbeat ~1 s later.
                self._schedule_locked()
            reply = self._build_reply(worker_id, worker, hb)
        if changed or freed or not was_fresh:
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
        return self._reconcile_unknown(c, worker_id, {s.job_id for s in jobs}, now) or any_terminal

    def _reconcile_unknown(self, c: sqlite3.Connection, worker_id: str, listed: set[str], now: float) -> bool:
        """Plan §6.2: an UNKNOWN job the worker's snapshot does not list goes back to ASSIGNED
        (never acked: never journaled, so it never ran; redelivered to the same worker) or
        FAILED(lost) (acked but gone: the worker lost its journal). True if any job ended."""
        ended = False
        for j in c.execute("SELECT job_id, acked FROM jobs WHERE worker_id=? AND state='UNKNOWN'",
                           (worker_id,)).fetchall():
            if j["job_id"] in listed:
                continue
            if j["acked"]:
                transition_job(c, j["job_id"], JobState.FAILED, "reconcile", ts=now, fail_reason="lost", end_ts=now,
                               error="acked job missing from the worker's snapshot after reconnect")
                ended = True
            else:
                transition_job(c, j["job_id"], JobState.ASSIGNED, "reconcile", ts=now)
        return ended

    def _build_reply(self, worker_id: str, worker: sqlite3.Row, hb: Heartbeat) -> HeartbeatReply:
        listed = {j.job_id for j in hb.jobs}
        assignments = [
            Assignment(job_id=r["job_id"], type=r["type"], params=json.loads(r["params_json"]),
                       input_bytes=r["input_bytes"], input_sha256=r["input_sha256"],
                       input_url=f"/api/v1/jobs/{r['job_id']}/input" if r["input_bytes"] else None,
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
        if sub.run_id is not None:
            r = self.db.one("SELECT end_ts FROM runs WHERE run_id=?", (sub.run_id,))
            if r is None or r["end_ts"] is not None:
                raise Invalid([{"loc": ["run_id"], "msg": f"run {sub.run_id!r} is not an open run"}])
        if sub.placement != "auto" and self.db.one("SELECT 1 FROM workers WHERE worker_id=?", (sub.placement,)) is None:
            raise Invalid([{"loc": ["placement"], "msg": f"unknown worker {sub.placement!r}"}])
        input_bytes = v.params.get("input_bytes", 0)
        # Blob generation is CPU work: keep it off the event loop (invariant 4).
        input_sha256 = await asyncio.to_thread(self.blobs.ensure, input_bytes) if input_bytes else None
        job_id = uuid.uuid4().hex
        now = time.time()
        async with self.db.lock:
            with self.db.tx() as c:
                c.execute(
                    """INSERT INTO jobs (job_id, type, params_json, mem_mb, max_runtime_s, timeout_s, placement,
                         state, submit_ts, run_id, trace_index, input_bytes, input_sha256)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, v.type, json.dumps(v.params), v.mem_mb, v.max_runtime_s, v.timeout_s,
                     sub.placement, JobState.PENDING.value, now, sub.run_id, sub.trace_index,
                     input_bytes, input_sha256),
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

    def job_input(self, job_id: str, authorize: Callable[[str], None]) -> Path:
        """Blob path for a job's input. `authorize(worker_id)` must raise unless the caller
        is the assigned worker; it runs before anything about the job's state is revealed."""
        row = self.db.one("SELECT worker_id, state, input_bytes FROM jobs WHERE job_id=?", (job_id,))
        if row is None or not row["worker_id"] or not row["input_bytes"]:
            raise NotFound(f"job {job_id!r} has no input for any worker")
        authorize(row["worker_id"])
        if row["state"] not in {s.value for s in COMMITTED}:
            raise Conflict(f"job {job_id!r} is {row['state']}; its input is no longer served")
        path = self.blobs.path(row["input_bytes"])
        if not path.is_file():
            raise NotFound(f"input blob for job {job_id!r} is missing on the controller")
        return path

    def list_jobs(self, *, state: str | None, worker: str | None, run_id: str | None,
                  limit: int, after: str | None, newest_first: bool = False) -> list[dict]:
        where, params = [], []
        for col, val in (("state", state), ("worker_id", worker), ("run_id", run_id)):
            if val:
                where.append(f"{col}=?")
                params.append(val)
        if after:
            where.append("rowid > (SELECT rowid FROM jobs WHERE job_id=?)")
            params.append(after)
        order = "DESC" if newest_first else "ASC"
        sql = "SELECT * FROM jobs" + (f" WHERE {' AND '.join(where)}" if where else "") + f" ORDER BY rowid {order} LIMIT ?"
        return [job_to_json(r) for r in self.db.all(sql, (*params, limit))]

    def list_decisions(self, *, run_id: str | None, limit: int) -> list[dict]:
        """Newest first, with the job's type and current state (Decisions view, plan §10)."""
        sql = """SELECT d.*, j.type, j.state AS job_state, j.mem_mb, j.placement, j.run_id
                 FROM decisions d JOIN jobs j ON j.job_id = d.job_id"""
        params: tuple = ()
        if run_id:
            sql += " WHERE j.run_id = ?"
            params = (run_id,)
        return [decision_to_json(r) for r in self.db.all(sql + " ORDER BY d.id DESC LIMIT ?", (*params, limit))]

    def overview(self, run_id: str | None = None) -> dict:
        return {**overview.compute(self.db.conn, time.time(), run_id), "scheduler": asdict(self.sched)}

    # ------------------------------------------------------------ runs (plan §8.2, §12)
    async def create_run(self, *, name: str, trace_hash: str | None, config: dict,
                         git_commit: str | None) -> dict:
        """Start a tagged run. 409 while any job is non-terminal or another run is open, so a
        run never inherits load from the previous one (plan §12.3). The policy recorded is
        the controller's active one, not what the harness believes it set."""
        now = time.time()
        async with self.db.lock:
            busy = self.db.one(f"SELECT COUNT(*) n FROM jobs WHERE state IN {_in(NON_TERMINAL)}")["n"]
            if busy:
                raise Conflict(f"{busy} job(s) are not terminal; a run starts only when the system is idle")
            open_run = self.db.one("SELECT run_id FROM runs WHERE end_ts IS NULL")
            if open_run:
                raise Conflict(f"run {open_run['run_id']!r} is still open; finish it first")
            run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + uuid.uuid4().hex[:6]
            cfg = {**config, "scheduler": asdict(self.sched)}
            with self.db.tx() as c:
                c.execute("""INSERT INTO runs (run_id, name, policy, trace_hash, config_json, git_commit, start_ts)
                             VALUES (?,?,?,?,?,?,?)""",
                          (run_id, name, self.sched.policy, trace_hash, json.dumps(cfg), git_commit, now))
                add_event(c, "run_started", ts=now, run_id=run_id, name=name, policy=self.sched.policy)
        return self.get_run(run_id)

    async def finish_run(self, run_id: str) -> dict:
        now = time.time()
        async with self.db.lock:
            row = self.db.one("SELECT end_ts FROM runs WHERE run_id=?", (run_id,))
            if row is None:
                raise NotFound(f"run {run_id!r} not found")
            if row["end_ts"] is None:
                with self.db.tx() as c:
                    c.execute("UPDATE runs SET end_ts=? WHERE run_id=?", (now, run_id))
                    add_event(c, "run_finished", ts=now, run_id=run_id)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        row = self.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if row is None:
            raise NotFound(f"run {run_id!r} not found")
        return runs.run_to_json(row)

    def list_runs(self, *, active: bool = False, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM runs" + (" WHERE end_ts IS NULL" if active else "") + " ORDER BY start_ts DESC LIMIT ?"
        now = time.time()
        out = []
        for r in self.db.all(sql, (limit,)):
            d = runs.run_to_json(r)
            # Controller-clock age of an open run: lets a remote helper (stress.py --follow-runs)
            # time itself against the run without synchronised clocks.
            d["elapsed_s"] = now - d["start_ts"] if d["end_ts"] is None else None
            out.append(d)
        return out

    def export_run(self, run_id: str, table: str) -> str:
        row = self.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if row is None:
            raise NotFound(f"run {run_id!r} not found")
        if table not in runs.EXPORT_TABLES:
            raise Invalid([{"loc": ["table"], "msg": f"one of {list(runs.EXPORT_TABLES)}"}])
        return runs.to_csv(*runs.export_rows(self.db.conn, row, table, time.time()))

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
        hb = (self.rt.get(r["worker_id"]) or WorkerRuntime()).host_busy
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
            "host_busy": hb.busy,
            "stale": bool(self.rt.get(r["worker_id"]) and self.rt[r["worker_id"]].stale),
            "foreign_cpu_mean": None if hb.mean is None else round(hb.mean, 1),
        }

    # ------------------------------------------------------------ scheduling
    async def schedule_pass(self) -> int:
        """One pass over PENDING jobs (plan §6.4): jobs pending longer than aging_s first, then
        FIFO; a job that can't be placed doesn't block the ones behind it. Each placement is one
        transaction (plan §6.3). Returns the number of placements made.

        Decision rows (DECISIONS D-015): one for every placement or reject, plus one at a job's
        first attempt when it stays PENDING; that job gets overflow=1 (plan §12.4)."""
        async with self.db.lock:
            return self._schedule_locked()

    def _schedule_locked(self) -> int:
        """The pass itself; the caller holds db.lock (schedule_pass, or a heartbeat that just
        freed a slot, so its reply already carries the next assignment: DECISIONS D-026)."""
        placed = 0
        aged_before = time.time() - self.sched.aging_s
        pending = self.db.all(
            "SELECT job_id FROM jobs WHERE state='PENDING' ORDER BY (submit_ts >= ?), submit_ts, rowid",
            (aged_before,))
        held: frozenset[str] = frozenset()  # workers reserved for an aged job (DECISIONS D-017)
        for (job_id,) in pending:
            t0 = time.perf_counter()
            with self.db.tx() as c:
                job = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if job is None or job["state"] != JobState.PENDING.value:
                    continue
                workers = self._worker_views(c)
                view = JobView(job_id, job["type"], job["mem_mb"], job["placement"])
                aged = job["submit_ts"] < aged_before
                d = self.policy.decide(view, list(workers.values()), held=held)
                if aged and d.worker_id is None and not d.structural_reject:
                    held |= {w for w, v in workers.items() if could_ever_run(view, v)}
                first = c.execute("SELECT 1 FROM decisions WHERE job_id=? LIMIT 1", (job_id,)).fetchone() is None
                if d.worker_id is None and not d.structural_reject and not first:
                    continue  # still PENDING; its first-attempt decision row already explains why
                now = time.time()
                c.execute(
                    """INSERT INTO decisions (job_id, ts, policy, worker_id, eligible_json, ineligible_json,
                         structural_reject, decision_us) VALUES (?,?,?,?,?,?,?,?)""",
                    (job_id, now, d.policy, d.worker_id, json.dumps(list(d.eligible)),
                     json.dumps(d.ineligible), int(d.structural_reject), (time.perf_counter() - t0) * 1e6),
                )
                if d.worker_id:
                    hostname = c.execute("SELECT hostname FROM workers WHERE worker_id=?",
                                         (d.worker_id,)).fetchone()["hostname"]
                    transition_job(c, job_id, JobState.ASSIGNED, "scheduler", ts=now, worker_id=d.worker_id,
                                   assign_ts=now, hostname=hostname)
                    placed += 1
                elif d.structural_reject:
                    transition_job(c, job_id, JobState.REJECTED, "scheduler", ts=now)
                else:
                    c.execute("UPDATE jobs SET overflow=1 WHERE job_id=?", (job_id,))
        return placed

    def _committed_usage(self, c: sqlite3.Connection | None = None) -> dict[str, tuple[int, int]]:
        """worker_id -> (slots used, MB committed), derived from jobs in COMMITTED states (plan §6.3)."""
        rows = (c or self.db.conn).execute(
            f"SELECT worker_id, COUNT(*) n, COALESCE(SUM(mem_mb),0) mem FROM jobs"
            f" WHERE worker_id IS NOT NULL AND state IN {_in(COMMITTED)} GROUP BY worker_id").fetchall()
        return {r["worker_id"]: (r["n"], r["mem"]) for r in rows}

    def _worker_views(self, c: sqlite3.Connection) -> dict[str, WorkerView]:
        used = self._committed_usage(c)
        views = {}
        for w in c.execute("SELECT * FROM workers ORDER BY worker_id").fetchall():
            wid = w["worker_id"]
            rt = self.rt.get(wid)
            n, mem = used.get(wid, (0, 0))
            views[wid] = WorkerView(
                worker_id=wid, tier=w["tier"], fresh=self.is_fresh(wid), draining=bool(w["draining"]),
                host_busy=bool(rt and rt.host_busy.busy),
                supported_types=frozenset(json.loads(w["supported_types_json"])),
                slots_total=len(json.loads(w["slots_json"])), slots_used=n, mem_budget_mb=w["mem_budget_mb"],
                mem_committed_mb=mem, mem_available_mb=int(((rt and rt.telemetry) or {}).get("mem_available_mb", 0)),
                mem_floor_mb=w["mem_floor_mb"])
        return views


def _snapshot_fields(s: JobSnapshot) -> dict:
    f = {k: getattr(s, k) for k in ("pid", "pid_create_time", "transfer_bytes", "transfer_s", "spawn_s",
                                    "exit_code", "error", "kill_latency_ms", "slot") if getattr(s, k) is not None}
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
