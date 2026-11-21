"""Classroom playground (DECISIONS D-027): one-click activities and hands-on tasks on the real
workers, with a live view, a plain-language story feed and a "Why here?" card per job.

Nothing here schedules anything: jobs go through the normal `service.submit` path and the
active policy decides. The "Cloud overflow queue" shown to the class is the controller's
real PENDING queue (jobs no local laptop can take right now; a Cloud tier would take them in
V2). A job running on a laptop that drops off stays UNKNOWN there and is never re-queued
(at-most-once, invariant 7). All teacher-facing wording lives here, not in the frontend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from controller.service import Conflict, Invalid, NotFound, ServiceError
from scheduler.views import TIER_RANK
from shared.catalog import MIB
from shared.schemas import JobSubmit
from shared.states import NON_TERMINAL, TERMINAL, JobState
from shared.traces import generate, variant_key

log = logging.getLogger("dwr.playground")

REPO = Path(__file__).resolve().parents[1]
REFERENCE_PATH = REPO / "experiments" / "reference.json"
BLOCK = "playground"

# Slot times measured on the Windows laptop on mains power (reference run 2026-11-21, D-026); used
# only when experiments/reference.json does not exist, to turn "load 1.5x" into a job rate.
DEFAULT_SERVICE_S = {
    'cpu_hash:{"iterations":1000000}': 1.18,
    'cpu_hash:{"iterations":2000000}': 1.93,
    'cpu_hash:{"iterations":500000}': 0.86,
    'data_digest:{"input_bytes":16777216,"rounds":2}': 0.77,
    'data_digest:{"input_bytes":4194304,"rounds":2}': 0.64,
    'data_digest:{"input_bytes":8388608,"rounds":2}': 0.73,
    'mem_touch:{"hold_s":1.0,"mem_mb":64}': 1.53,
    'mem_touch:{"hold_s":2.0,"mem_mb":128}': 2.61,
    'mem_touch:{"hold_s":3.0,"mem_mb":256}': 3.67,
}

LABELS = {
    "queue_title": "Cloud overflow queue",
    "queue_note": ("Jobs that no local laptop can take right now. In V2 a Cloud tier would receive them; "
                   "here they wait and the next laptop that frees a slot picks them up."),
    "queue_empty": "Empty: the local laptops are keeping up.",
    "offline_tile": "offline: still running there, result pending",
    "demand_label": "Cloud time needed",
}

PHASE_TEXT = {"ASSIGNED": "sent to the laptop…", "ACCEPTED": "starting…", "UNKNOWN": LABELS["offline_tile"]}

REASON_TEXT = {
    "stale": "offline",
    "draining": "paused by the operator",
    "unsupported_type": "can't run this kind of job",
    "no_free_slot": "all its slots are busy",
    "mem_budget": "its job-memory budget is used up",
    "mem_floor": "too little free RAM left",
    "host_busy": "busy with other programs",
    "not_requested": "not the laptop you asked for",
    "reserved_for_aged": "held for a job that has waited longer",
}

POLICY_TEXT = {
    "LocalFirst": "LocalFirst tries the nearest laptop first and moves on only when it is full",
    "EdgeOnly": "EdgeOnly lets only the Edge laptop run jobs (the 'no Fog' baseline)",
    "FogOnly": "FogOnly sends everything to the Fog laptop",
    "RoundRobin": "RoundRobin lets the laptops take turns",
}

SCENARIOS: list[dict] = [
    {"id": "quiet", "title": "Quiet day", "policy": "LocalFirst", "load": 0.3, "mix": "mixed",
     "arrival": "poisson", "duration_s": 60,
     "watch": "Light work: almost everything runs on the Edge laptop; Fog only helps at the odd "
              "moment when both Edge slots happen to be busy."},
    {"id": "saturate", "title": "Edge saturates", "policy": "LocalFirst", "load": 1.5, "mix": "mixed",
     "arrival": "poisson", "duration_s": 90,
     "watch": "More work than Edge can take: jobs spill over to the Fog laptop."},
    {"id": "rush", "title": "Rush hour", "policy": "LocalFirst", "load": 2.5, "mix": "mixed",
     "arrival": "bursts", "duration_s": 45,
     "watch": "Bursts bigger than both laptops: the Cloud queue grows, then drains as slots free up."},
    {"id": "no_fog", "title": "Without Fog", "policy": "EdgeOnly", "load": 1.5, "mix": "mixed",
     "arrival": "poisson", "duration_s": 90, "seed_of": "saturate",
     "watch": "Same jobs as 'Edge saturates' but Edge alone: compare the scoreboards below."},
    {"id": "fog_busy", "title": "Fog gets busy", "policy": "LocalFirst", "load": 1.5, "mix": "mixed",
     "arrival": "poisson", "duration_s": 100, "stress": {"at_s": 20, "for_s": 40},
     "watch": "At +20 s another program loads the Fog laptop: it turns HOST BUSY and new jobs avoid it.",
     "needs": "stress.py --follow-runs --block playground running on the Fog laptop"},
    {"id": "both_busy", "title": "Both laptops busy", "policy": "LocalFirst", "load": 1.2, "mix": "mixed",
     "arrival": "poisson", "duration_s": 120, "stress": {"at_s": 20, "for_s": 40},
     "stress_edge": {"at_s": 20, "for_s": 40},
     "watch": "Both laptops get busy with other work: nothing new can start, the queue builds, "
              "then both pick jobs from the queue when they are free again.",
     "needs": "stress.py --follow-runs --block playground running on the Fog laptop"},
    {"id": "disconnect", "title": "Fog disconnects", "policy": "LocalFirst", "load": 1.3, "mix": "mixed",
     "arrival": "poisson", "duration_s": 150,
     "cues": [{"at_s": 20, "for_s": 15, "text": "Now turn OFF the Fog laptop's Wi-Fi"},
              {"at_s": 70, "for_s": 15, "text": "Now turn the Fog laptop's Wi-Fi back ON"}],
     "watch": "Pull the plug on Fog mid-job: its job waits there (never run twice), new jobs queue; "
              "on reconnect the result arrives and Fog picks jobs from the queue."},
    {"id": "cancel", "title": "Cancel a long job", "policy": "LocalFirst", "long_jobs": True,
     "watch": "A 30 s job starts on each laptop. Click 'cancel' on a tile: the process is killed and "
              "the kill is verified."},
]

TASKS: list[dict] = [
    {"id": "small_hash", "label": "Small hash", "type": "cpu_hash", "params": {"iterations": 500_000}},
    {"id": "big_hash", "label": "Big hash", "type": "cpu_hash", "params": {"iterations": 2_000_000}},
    {"id": "memory", "label": "Memory 256 MB, 10 s", "type": "mem_touch", "params": {"mem_mb": 256, "hold_s": 10.0}},
    {"id": "data", "label": "Data job 16 MB", "type": "data_digest",
     "params": {"input_bytes": 16 * MIB, "rounds": 2}},
    {"id": "long", "label": "Long job (30 s)", "type": "mem_touch", "params": {"mem_mb": 64, "hold_s": 30.0}},
]
CUSTOM_LIMITS = {"load": (0.25, 3.0), "duration_s": (30, 180)}


def job_label(type_: str, params: dict) -> str:
    if type_ == "cpu_hash":
        return f"hash {params['iterations'] / 1e6:g}M"
    if type_ == "mem_touch":
        return f"memory {params['mem_mb']} MB · {params['hold_s']:g} s"
    if type_ == "data_digest":
        return f"data {params['input_bytes'] / MIB:g} MB × {params['rounds']}"
    return type_


def reasons_text(ineligible: dict) -> str:
    return " · ".join(f"{w}: {REASON_TEXT.get(r, r)}" for w, r in sorted(ineligible.items()))


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


@dataclass
class Activity:
    run_id: str
    kind: str                     # scenario | custom | free_play
    title: str
    watch: str
    policy: str
    started_ts: float
    duration_s: float = 0.0
    total: int = 0
    submitted: int = 0
    next_index: int = 0
    cues: list = field(default_factory=list)
    clicks: dict = field(default_factory=dict)      # job_id -> click number (hands-on)
    task: asyncio.Task | None = None
    stress_proc: subprocess.Popen | None = None
    finished: bool = False
    finished_ts: float | None = None


class Playground:
    def __init__(self, service):
        self.service = service
        self.active: Activity | None = None
        self.last: Activity | None = None
        self._finisher: asyncio.Task | None = None

    # ------------------------------------------------------------ helpers
    @property
    def db(self):
        return self.service.db

    def reference(self) -> dict:
        try:
            ref = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
            if ref.get("mean_service_s"):
                return ref
        except (OSError, ValueError):
            pass
        edge = [w for w in self.service.list_workers() if w["tier"] == "edge"]
        return {"edge_slots": edge[0]["slots_total"] if edge else 2, "mean_service_s": DEFAULT_SERVICE_S,
                "source": "built-in default"}

    def expected_s(self, type_: str, params: dict) -> float | None:
        return self.reference().get("mean_service_s", {}).get(variant_key(type_, params))

    def catalog(self) -> dict:
        ref = self.reference()
        tasks = []
        for t in TASKS:
            exp = ref.get("mean_service_s", {}).get(variant_key(t["type"], t["params"]))
            hint = f"≈ {exp:.1f} s on Edge" if exp else (
                f"≈ {t['params']['hold_s']:g} s" if "hold_s" in t["params"] else "")
            tasks.append({**t, "hint": hint})
        return {"scenarios": [{**{k: s.get(k) for k in ("id", "title", "watch", "policy", "load", "duration_s", "needs")},
                               "needs_short": "needs the stress helper on Fog" if s.get("needs") else None}
                              for s in SCENARIOS],
                "tasks": tasks, "labels": LABELS, "custom_limits": CUSTOM_LIMITS,
                "policies": sorted(POLICY_TEXT), "mixes": ["cpu", "mem", "data", "mixed"],
                "reference": ref.get("source", "experiments/reference.json")}

    # ------------------------------------------------------------ lifecycle
    async def recover(self) -> None:
        """At controller start: an activity cannot survive a restart. Cancel its unfinished jobs
        (requested, like any cancel) and close its run, so a class never inherits old work."""
        for r in self.service.list_runs(active=True):
            if (r.get("config") or {}).get("block") != BLOCK:
                continue
            left = self.db.all(f"SELECT job_id FROM jobs WHERE run_id=? AND state IN "
                               f"({','.join(repr(s.value) for s in NON_TERMINAL)})", (r["run_id"],))
            for (job_id,) in left:
                try:
                    await self.service.cancel(job_id)
                except ServiceError:
                    pass
            await self.service.finish_run(r["run_id"])
            log.info("closed playground run %s left open by a previous controller (%d jobs cancelled)",
                     r["run_id"], len(left))

    def shutdown(self) -> None:
        if self.active:
            self._kill_stress(self.active)
            if self.active.task:
                self.active.task.cancel()

    def _busy(self) -> int:
        return self.db.one(f"SELECT COUNT(*) n FROM jobs WHERE state IN "
                           f"({','.join(repr(s.value) for s in NON_TERMINAL)})")["n"]

    async def _open(self, kind: str, title: str, watch: str, policy: str, host_busy: bool, config: dict) -> Activity:
        if self.active and not self.active.finished:
            if self.active.kind == "free_play" and not self._run_busy(self.active.run_id):
                await self._close(self.active)
            else:
                raise Conflict(f"'{self.active.title}' is still running: stop it first")
        if self._busy():
            raise Conflict("jobs from outside the playground are still running; wait for them or cancel them")
        await self.service.put_scheduler({"policy": policy, "host_busy_enabled": host_busy})
        run = await self.service.create_run(name=f"playground: {title}", trace_hash=None, git_commit=None,
                                            config={"block": BLOCK, "playground": kind, **config})
        self.active = Activity(run_id=run["run_id"], kind=kind, title=title, watch=watch, policy=policy,
                               started_ts=run["start_ts"])
        return self.active

    def _run_busy(self, run_id: str) -> int:
        return self.db.one(f"SELECT COUNT(*) n FROM jobs WHERE run_id=? AND state IN "
                           f"({','.join(repr(s.value) for s in NON_TERMINAL)})", (run_id,))["n"]

    async def _close(self, a: Activity) -> None:
        self._kill_stress(a)
        a.finished = True
        a.finished_ts = a.finished_ts or time.time()
        await self.service.finish_run(a.run_id)
        self.last = a
        if self.active is a:
            self.active = None

    def _kill_stress(self, a: Activity) -> None:
        p, a.stress_proc = a.stress_proc, None
        if p is None or p.poll() is not None:
            return
        try:
            root = psutil.Process(p.pid)
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:
            return
        for q in reversed(procs):
            try:
                q.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(procs, timeout=5)

    # ------------------------------------------------------------ activities
    async def start(self, *, scenario: str | None = None, custom: dict | None = None) -> dict:
        if scenario:
            s = next((x for x in SCENARIOS if x["id"] == scenario), None)
            if s is None:
                raise NotFound(f"unknown scenario {scenario!r}")
            kind, title, watch = "scenario", s["title"], s["watch"]
        elif custom is not None:
            s = self._custom(custom)
            kind, title, watch = "custom", s["title"], s["watch"]
        else:
            raise Invalid([{"loc": ["scenario"], "msg": "give a scenario id or a custom activity"}])
        config = {"scenario": s.get("id"), "load": s.get("load"), "mix": s.get("mix")}
        if s.get("stress"):
            config["stress"] = s["stress"]          # stress.py --follow-runs on the Fog laptop
        a = await self._open(kind, title, watch, s["policy"], True, config)
        a.cues = s.get("cues", [])
        rows = self._rows(s)
        a.total, a.duration_s = len(rows), float(s.get("duration_s", 0) or 0)
        if s.get("stress_edge"):
            se = s["stress_edge"]
            a.stress_proc = subprocess.Popen(
                [sys.executable, "-m", "experiments.stress", "--cpus", "all", "--target-host-pct", "85",
                 "--delay", str(se["at_s"]), "--duration", str(se["for_s"]), "--env-file", "none"],
                cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        a.task = asyncio.create_task(self._feed(a, rows), name=f"playground-{a.run_id}")
        return self.activity()

    def _custom(self, c: dict) -> dict:
        errors = []
        try:
            load = float(c.get("load", 1.0))
            duration = float(c.get("duration_s", 60))
        except (TypeError, ValueError):
            raise Invalid([{"loc": ["custom"], "msg": "load and duration_s must be numbers"}]) from None
        lo, hi = CUSTOM_LIMITS["load"]
        if not lo <= load <= hi:
            errors.append({"loc": ["load"], "msg": f"must be in [{lo}, {hi}]"})
        lo, hi = CUSTOM_LIMITS["duration_s"]
        if not lo <= duration <= hi:
            errors.append({"loc": ["duration_s"], "msg": f"must be in [{lo}, {hi}]"})
        mix = c.get("mix", "mixed")
        if mix not in ("cpu", "mem", "data", "mixed"):
            errors.append({"loc": ["mix"], "msg": "cpu, mem, data or mixed"})
        policy = c.get("policy", "LocalFirst")
        if policy not in POLICY_TEXT:
            errors.append({"loc": ["policy"], "msg": f"one of {sorted(POLICY_TEXT)}"})
        if errors:
            raise Invalid(errors)
        return {"title": f"Custom: {load:g}× load, {mix}, {policy}", "policy": policy, "load": load, "mix": mix,
                "arrival": c.get("arrival", "poisson"), "duration_s": duration, "seed": int(time.time()),
                "watch": f"Your own activity: {load:g}× the Edge laptop's capacity for {duration:g} s."}

    def _rows(self, s: dict) -> list[dict]:
        if s.get("long_jobs"):
            workers = sorted(self.service.list_workers(), key=lambda w: TIER_RANK.get(w["tier"], 99))
            return [{"index": i, "t_s": 0.0, "type": "mem_touch", "params": {"mem_mb": 64, "hold_s": 30.0},
                     "placement": w["worker_id"]} for i, w in enumerate(workers) if w["fresh"]]
        seed = s.get("seed") or zlib.crc32((s.get("seed_of") or s["id"]).encode())
        return generate(reference=self.reference(), load=s["load"], mix=s["mix"], arrival=s["arrival"],
                        duration_s=s["duration_s"], seed=seed)

    async def _feed(self, a: Activity, rows: list[dict]) -> None:
        """Open-loop submission, then wait for the run's jobs and close it."""
        t0 = time.monotonic()
        try:
            for r in rows:
                delay = t0 + r["t_s"] - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._submit(a, r["type"], r["params"], r.get("placement", "auto"))
                a.submitted += 1
            while self._run_busy(a.run_id):
                await asyncio.sleep(1.0)
            await self._close(a)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("playground activity %s failed", a.run_id)

    async def _submit(self, a: Activity, type_: str, params: dict, placement: str) -> str:
        idx = a.next_index
        a.next_index += 1
        res = await self.service.submit(JobSubmit(type=type_, params=params, placement=placement,
                                                  run_id=a.run_id, trace_index=idx))
        return res["job_id"]

    async def stop(self) -> dict:
        a = self.active
        if a is None:
            return self.activity()
        if a.task:
            a.task.cancel()
        self._kill_stress(a)
        for (job_id,) in self.db.all(f"SELECT job_id FROM jobs WHERE run_id=? AND state IN "
                                     f"({','.join(repr(s.value) for s in NON_TERMINAL)})", (a.run_id,)):
            try:
                await self.service.cancel(job_id)
            except ServiceError:
                pass

        async def finish_when_quiet():
            for _ in range(60):
                if not self._run_busy(a.run_id):
                    break
                await asyncio.sleep(0.5)
            await self._close(a)

        self._finisher = asyncio.create_task(finish_when_quiet())
        a.finished = True
        a.finished_ts = time.time()
        return self.activity()

    # ------------------------------------------------------------ hands-on tasks
    async def submit_task(self, *, task_id: str | None = None, type_: str | None = None, params: dict | None = None,
                          placement: str = "auto", count: int = 1) -> dict:
        if task_id:
            t = next((x for x in TASKS if x["id"] == task_id), None)
            if t is None:
                raise NotFound(f"unknown task {task_id!r}")
            type_, params, label = t["type"], t["params"], t["label"]
        elif type_:
            label = job_label(type_, params or {})
        else:
            raise Invalid([{"loc": ["task_id"], "msg": "give a task id or a type with params"}])
        if not 1 <= count <= 10:
            raise Invalid([{"loc": ["count"], "msg": "1 to 10"}])
        a = self.active if self.active and not self.active.finished else None
        if a is None:
            a = await self._open("free_play", "Free play", "Launch tasks yourself and see where each one goes.",
                                 self.service.sched.policy, self.service.sched.host_busy_enabled, {})
        ids = []
        for _ in range(count):
            jid = await self._submit(a, type_, params or {}, placement)
            a.clicks[jid] = len(a.clicks) + 1
            ids.append(jid)
        await asyncio.sleep(0.05)   # let the scheduling pass run so the first card already says where it went
        return {"label": label, "jobs": [self.explain(j) for j in ids], "activity": self.activity()}

    # ------------------------------------------------------------ explanations
    def _workers(self) -> dict[str, dict]:
        return {w["worker_id"]: w for w in self.service.list_workers()}

    def _number(self, job) -> int:
        a = self.active or self.last
        if a and job["job_id"] in a.clicks:
            return a.clicks[job["job_id"]]
        return (job["trace_index"] or 0) + 1

    def explain(self, job_id: str) -> dict:
        j = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if j is None:
            raise NotFound(f"job {job_id!r} not found")
        decisions = self.db.all("SELECT * FROM decisions WHERE job_id=? ORDER BY id", (job_id,))
        events = self.db.all("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,))
        workers = self._workers()
        params = json.loads(j["params_json"])
        label = job_label(j["type"], params)
        a = self.active or self.last
        clicked = a is not None and job_id in a.clicks
        n = self._number(j)
        who = f"Your {_ordinal(n)} click ({label})" if clicked else f"Job #{n} ({label})"
        lines: list[str] = []
        headline = f"{who}: waiting for the scheduler…"
        if decisions:
            d0 = decisions[0]
            inel = json.loads(d0["ineligible_json"])
            eligible = json.loads(d0["eligible_json"])
            if d0["structural_reject"]:
                headline = f"{who} was rejected: no laptop could ever run it."
                lines.append(reasons_text(inel))
            elif d0["worker_id"]:
                w = workers.get(d0["worker_id"], {})
                others_full = {k: v for k, v in inel.items()
                               if TIER_RANK.get(workers.get(k, {}).get("tier"), 99) < TIER_RANK.get(w.get("tier"), 99)}
                why = (f"{reasons_text(others_full)}, so " if others_full else "a slot was free, so ")
                headline = f"{who}: {why}{d0['worker_id']} ({w.get('tier', '?')}) took it straight away."
                lines.append(POLICY_TEXT.get(d0["policy"], d0["policy"]) + ".")
                rest = [x for x in eligible if x != d0["worker_id"]]
                if rest:
                    lines.append(f"Also free: {', '.join(rest)} (the policy preferred {d0['worker_id']}).")
                if inel and not others_full:
                    lines.append("Not usable: " + reasons_text(inel) + ".")
            else:
                headline = (f"{who}: no laptop could take it ({reasons_text(inel)}), so it went to the "
                            f"{LABELS['queue_title']}. A Cloud tier would take it here.")
            later = [d for d in decisions[1:] if d["worker_id"]]
            if later and not d0["worker_id"]:
                waited = later[0]["ts"] - j["submit_ts"]
                lines.append(f"Picked from the queue by {later[0]['worker_id']} after {waited:.1f} s.")
        if j["worker_id"] and j["state"] in ("RUNNING", "SUCCEEDED", "FAILED", "KILLED") and j["pid"]:
            w = workers.get(j["worker_id"], {})
            cores = ""
            if j["slot"] is not None and w.get("slots") and j["slot"] < len(w["slots"]):
                cores = f", slot {j['slot']} (core {'+'.join(map(str, w['slots'][j['slot']]['cpus']))})"
            lines.append(f"Ran on {j['hostname']} as PID {j['pid']}{cores}.")
        states = [e["to_state"] for e in events]
        if "UNKNOWN" in states:
            if j["state"] == "UNKNOWN":
                lines.append(f"{j['worker_id']} lost contact while running it: it is still running there, "
                             f"result pending. It is never re-run elsewhere (at most once).")
            else:
                lines.append(f"{j['worker_id']} lost contact while running it; the result arrived after it "
                             f"reconnected.")
        if j["state"] in {s.value for s in TERMINAL}:
            lines.append(self._outcome(j))
        timeline = [{"t": round(e["ts"] - j["submit_ts"], 2), "state": e["to_state"], "by": e["trigger"],
                     "reason": e["reason"]} for e in events]
        return {"job_id": job_id, "n": n, "label": label, "state": j["state"], "worker_id": j["worker_id"],
                "headline": headline, "lines": lines, "timeline": timeline, "clicked": clicked}

    @staticmethod
    def _outcome(j) -> str:
        if j["state"] == "SUCCEEDED":
            return f"Finished: SUCCEEDED, {j['exec_s']:.1f} s of work, {j['end_ts'] - j['submit_ts']:.1f} s in total."
        if j["state"] == "KILLED":
            v = "verified gone" if j["termination_verified"] else "NOT verified"
            lat = f" in {j['kill_latency_ms']:.0f} ms" if j["kill_latency_ms"] is not None else ""
            return f"Killed ({j['kill_reason']}): process {v}{lat}."
        if j["state"] == "FAILED":
            return f"Failed ({j['fail_reason']})."
        return f"Ended: {j['state']}."

    # ------------------------------------------------------------ story feed
    def narrate(self, after_ts: float, limit: int = 40) -> tuple[list[dict], float]:
        a = self.active or self.last
        run_id = a.run_id if a else None
        workers = self._workers()
        items: list[tuple[float, str, str | None]] = []
        if run_id:
            seen_first: set[str] = set()
            for d in self.db.all(
                    """SELECT d.*, j.type, j.params_json, j.trace_index, j.submit_ts FROM decisions d
                       JOIN jobs j ON j.job_id = d.job_id WHERE j.run_id = ? ORDER BY d.id""", (run_id,)):
                first = d["job_id"] not in seen_first
                seen_first.add(d["job_id"])
                if d["ts"] <= after_ts:
                    continue
                tag = self._tag(d)
                inel = json.loads(d["ineligible_json"])
                if d["structural_reject"]:
                    items.append((d["ts"], f"{tag} rejected: {reasons_text(inel)}", d["job_id"]))
                elif d["worker_id"] and first:
                    w = workers.get(d["worker_id"], {})
                    lower = {k: v for k, v in inel.items()
                             if TIER_RANK.get(workers.get(k, {}).get("tier"), 99) < TIER_RANK.get(w.get("tier"), 99)}
                    pre = f"{' · '.join(f'{k} full' if v == 'no_free_slot' else f'{k} {REASON_TEXT.get(v, v)}' for k, v in sorted(lower.items()))} → " if lower else ""
                    items.append((d["ts"], f"{pre}{tag} → {d['worker_id']}", d["job_id"]))
                elif d["worker_id"]:
                    items.append((d["ts"], f"{tag} picked from the queue by {d['worker_id']} after "
                                           f"{d['ts'] - d['submit_ts']:.1f} s", d["job_id"]))
                else:
                    items.append((d["ts"], f"{tag} → {LABELS['queue_title']} ({reasons_text(inel)})", d["job_id"]))
            for e in self.db.all(
                    """SELECT e.*, j.type, j.params_json, j.trace_index, j.worker_id, j.exec_s, j.kill_reason,
                              j.fail_reason, j.termination_verified, j.kill_latency_ms, j.job_id
                       FROM job_events e JOIN jobs j ON j.job_id = e.job_id
                       WHERE j.run_id = ? AND e.ts > ? AND e.to_state IN
                         ('UNKNOWN','SUCCEEDED','FAILED','KILLED','CANCELLED') ORDER BY e.id""", (run_id, after_ts)):
                tag = self._tag(e)
                if e["to_state"] == "UNKNOWN":
                    text = f"{e['worker_id']} lost contact: {tag} is still running there, result pending"
                elif e["from_state"] == "UNKNOWN":
                    text = f"{tag} result arrived after {e['worker_id']} reconnected: {e['to_state']}"
                elif e["to_state"] == "SUCCEEDED":
                    text = f"{tag} finished on {e['worker_id']} ({e['exec_s']:.1f} s)" if e["exec_s"] else \
                        f"{tag} finished on {e['worker_id']}"
                elif e["to_state"] == "KILLED":
                    v = "verified" if e["termination_verified"] else "NOT verified"
                    text = f"{tag} killed on {e['worker_id']} ({e['kill_reason']}, {v})"
                elif e["to_state"] == "FAILED":
                    text = f"{tag} failed ({e['fail_reason']})"
                else:
                    text = f"{tag} cancelled"
                items.append((e["ts"], text, e["job_id"]))
        for ev in self.db.all("SELECT * FROM events WHERE ts > ? AND kind IN "
                              "('host_busy','host_free','worker_stale','worker_fresh') ORDER BY id", (after_ts,)):
            det = json.loads(ev["details_json"] or "{}")
            w = ev["worker_id"]
            text = {
                "host_busy": f"{w} is busy with other programs (CPU {det.get('mean_foreign_cpu_pct', '?')}%) → "
                             f"no new jobs for it",
                "host_free": f"{w} is free again → it takes jobs from the queue",
                "worker_stale": f"{w} went offline (no heartbeat for {det.get('heartbeat_age_s', '?')} s)",
                "worker_fresh": f"{w} is back online",
            }[ev["kind"]]
            items.append((ev["ts"], text, None))
        items.sort(key=lambda x: x[0])
        items = items[-limit:]
        cursor = max([after_ts] + [t for t, _, _ in items])
        return [{"ts": t, "text": s, "job_id": jid} for t, s, jid in items], cursor

    def _tag(self, row) -> str:
        n = (row["trace_index"] or 0) + 1
        a = self.active or self.last
        if a and row["job_id"] in a.clicks:
            n = a.clicks[row["job_id"]]
        return f"#{n} {job_label(row['type'], json.loads(row['params_json']))}"

    # ------------------------------------------------------------ live view
    def activity(self) -> dict | None:
        a = self.active or self.last
        if a is None:
            return None
        elapsed = (a.finished_ts or time.time()) - a.started_ts   # frozen once the activity ends
        cue = next((c for c in a.cues if c["at_s"] <= elapsed < c["at_s"] + c.get("for_s", 15)), None)
        return {"run_id": a.run_id, "kind": a.kind, "title": a.title, "watch": a.watch, "policy": a.policy,
                "policy_text": POLICY_TEXT.get(a.policy, a.policy), "running": not a.finished,
                "elapsed_s": round(elapsed, 1), "duration_s": a.duration_s, "total": a.total,
                "submitted": a.submitted, "cue": cue["text"] if cue and not a.finished else None,
                "edge_stress": a.stress_proc is not None and a.stress_proc.poll() is None}

    def _tile(self, j, workers: dict, now: float) -> dict:
        params = json.loads(j["params_json"])
        stats = {}
        rt = self.service.rt.get(j["worker_id"]) if j["worker_id"] else None
        if rt:
            stats = rt.job_stats.get(j["job_id"], {})
        w = workers.get(j["worker_id"], {})
        cores = None
        if j["slot"] is not None and w.get("slots") and j["slot"] < len(w["slots"]):
            cores = w["slots"][j["slot"]]["cpus"]
        started = j["start_ts"] or j["assign_ts"] or j["submit_ts"]
        queued = self.db.one("SELECT 1 FROM decisions WHERE job_id=? AND worker_id IS NULL LIMIT 1", (j["job_id"],))
        phase = PHASE_TEXT.get(j["state"])
        if j["state"] == "ACCEPTED" and j["input_bytes"]:
            phase = f"downloading its {j['input_bytes'] / MIB:g} MB input from the controller…"
        return {"job_id": j["job_id"], "n": self._number(j), "type": j["type"], "label": job_label(j["type"], params),
                "state": j["state"], "phase": phase, "pid": j["pid"], "slot": j["slot"], "cores": cores,
                "cpu_pct": stats.get("cpu_pct"), "rss_mb": stats.get("rss_mb"),
                "elapsed_s": round(now - started, 1), "expected_s": self.expected_s(j["type"], params),
                "offline": j["state"] == "UNKNOWN",
                "from_queue_after_s": round((j["assign_ts"] or now) - j["submit_ts"], 1) if queued else None,
                "cancel_requested": bool(j["cancel_requested"])}

    def live(self, after_ts: float = 0.0) -> dict:
        now = time.time()
        workers = self._workers()
        active_rows = self.db.all(
            f"SELECT * FROM jobs WHERE worker_id IS NOT NULL AND state IN "
            f"('ASSIGNED','ACCEPTED','RUNNING','UNKNOWN') ORDER BY assign_ts")
        lanes: dict[str, dict] = {}
        for wid, w in workers.items():
            tel = w.get("telemetry") or {}
            lanes[wid] = {
                "worker_id": wid, "tier": w["tier"], "tier_rank": TIER_RANK.get(w["tier"], 99),
                "hostname": w["hostname"], "os": w["os"], "fresh": w["fresh"], "stale": w.get("stale", False),
                "host_busy": w["host_busy"], "draining": w["draining"], "cpu_pct": tel.get("cpu_pct"),
                "foreign_cpu_mean": w.get("foreign_cpu_mean"),
                "mem_pct": round(100 * (1 - tel["mem_available_mb"] / tel["mem_total_mb"]), 0)
                if tel.get("mem_total_mb") else None,
                "heartbeat_age_s": None if w["heartbeat_age_s"] is None else round(w["heartbeat_age_s"], 1),
                "slots": [{"slot": s["slot"], "cpus": s["cpus"], "job": None} for s in w["slots"]],
                "arriving": [],
            }
        for j in active_rows:
            lane = lanes.get(j["worker_id"])
            if lane is None:
                continue
            tile = self._tile(j, workers, now)
            if j["slot"] is not None and j["slot"] < len(lane["slots"]) and lane["slots"][j["slot"]]["job"] is None:
                lane["slots"][j["slot"]]["job"] = tile
            else:
                lane["arriving"].append(tile)
        # A job sent to a laptop but not started yet already holds one of its slots (the controller
        # reserved it); show it in a free lane rather than under the card.
        for lane in lanes.values():
            free = [s for s in lane["slots"] if s["job"] is None]
            while free and lane["arriving"]:
                free.pop(0)["job"] = lane["arriving"].pop(0)
        queue = [{"job_id": j["job_id"], "n": self._number(j), "label": job_label(j["type"], json.loads(j["params_json"])),
                  "waiting_s": round(now - j["submit_ts"], 1), "overflow": bool(j["overflow"])}
                 for j in self.db.all("SELECT * FROM jobs WHERE state='PENDING' ORDER BY submit_ts, rowid")]
        a = self.active or self.last
        board, finished = None, []
        if a:
            board = self.scoreboard(a.run_id)
            finished = [{"job_id": j["job_id"], "n": self._number(j),
                         "label": job_label(j["type"], json.loads(j["params_json"])), "state": j["state"],
                         "reason": j["fail_reason"] or j["kill_reason"], "worker_id": j["worker_id"],
                         "exec_s": j["exec_s"], "total_s": None if j["end_ts"] is None else round(j["end_ts"] - j["submit_ts"], 1)}
                        for j in self.db.all(
                            f"SELECT * FROM jobs WHERE run_id=? AND state IN "
                            f"({','.join(repr(s.value) for s in TERMINAL)}) ORDER BY end_ts DESC LIMIT 12", (a.run_id,))]
        feed, cursor = self.narrate(after_ts)
        return {"now": now, "activity": self.activity(), "labels": LABELS,
                "workers": sorted(lanes.values(), key=lambda l: (l["tier_rank"], l["worker_id"])),
                "queue": queue, "finished": finished, "scoreboard": board, "feed": feed, "cursor": cursor,
                "compare": self.compare()}

    def scoreboard(self, run_id: str) -> dict:
        from controller import overview
        o = overview.compute(self.db.conn, time.time(), run_id)
        comp = sorted(r["c"] for r in self.db.all(
            "SELECT end_ts - submit_ts c FROM jobs WHERE run_id=? AND state='SUCCEEDED'", (run_id,)))
        p95 = comp[min(len(comp) - 1, int(round(0.95 * (len(comp) - 1))))] if comp else None
        return {"total": o["total_jobs"], "completed": o["completed"], "by_state": o["by_state"],
                "tiers": o["tiers"], "offload_pct": o["local_offload_pct_by_count"], "waiting": o["backlog"],
                "peak_queue": o["peak_backlog"], "overflow_jobs": o["overflow_jobs"],
                "overflow_demand_s": o["overflow_demand_s"], "p95_s": None if p95 is None else round(p95, 1),
                "failed": o["by_state"].get("FAILED", 0) + o["by_state"].get("KILLED", 0)}

    def compare(self, limit: int = 6) -> list[dict]:
        out = []
        for r in self.service.list_runs(limit=50):
            if (r.get("config") or {}).get("block") != BLOCK or r["end_ts"] is None:
                continue
            b = self.scoreboard(r["run_id"])
            if not b["total"]:
                continue
            out.append({"run_id": r["run_id"], "title": r["name"].removeprefix("playground: "), "policy": r["policy"],
                        **{k: b[k] for k in ("total", "completed", "p95_s", "overflow_jobs", "overflow_demand_s",
                                             "offload_pct", "peak_queue")}})
            if len(out) >= limit:
                break
        return out
