"""Worker agent (plan §5): register, heartbeat loop, reply handling.

Run: python -m worker.agent [--env-file PATH]   (PATH "-" = environment only)

Threads: the main thread runs the heartbeat loop (at most one request in flight);
a supervisor thread spawns and reaps jobs. They share the journal.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import platform
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import httpx
import psutil

from shared.envfile import load_env
from shared.schemas import (
    SCHEMA_VERSION, Assignment, HeartbeatReply, JobValidationError, RegisterReply, validate_assignment,
)
from worker.journal import Journal, row_to_snapshot
from worker.recovery import recover
from worker.supervisor import Supervisor, TransferCancelled
from worker.telemetry import TelemetrySampler
from worker.workloads import IMPLEMENTED

log = logging.getLogger("dwr.agent")

AGENT_VERSION = "1.0.0-m4"
REQUEST_TIMEOUT_S = 3.0
# Input download: connect quickly, tolerate slow Wi-Fi between chunks.
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
DOWNLOAD_CHUNK = 1024 * 1024
REGISTER_RETRY_S = 2.0
PRUNE_EVERY_S = 600.0


class ConfigError(ValueError):
    pass


class FatalAgentError(RuntimeError):
    """The controller refused this agent (bad token, incompatible schema)."""


def parse_slot_cpus(spec: str) -> list[list[int]]:
    """"1;2" -> [[1], [2]];  "1,2;3,4" -> [[1, 2], [3, 4]]."""
    slots = []
    for part in spec.split(";"):
        cpus = [int(c) for c in part.split(",") if c.strip()]
        if not cpus:
            raise ValueError(f"empty slot in {spec!r}")
        slots.append(cpus)
    flat = [c for s in slots for c in s]
    if len(flat) != len(set(flat)):
        raise ValueError(f"a core appears in more than one slot: {spec!r}")
    return slots


@dataclass(frozen=True)
class AgentConfig:
    worker_id: str
    tier: str
    controller_url: str
    token: str
    slot_cpus: list[list[int]]
    mem_budget_mb: int
    mem_floor_mb: int
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "AgentConfig":
        errors: list[str] = []

        def req(key: str) -> str:
            v = env.get(key, "").strip()
            if not v:
                errors.append(f"{key} is required")
            return v

        def integer(key: str) -> int:
            v = req(key)
            try:
                return int(v) if v else 0
            except ValueError:
                errors.append(f"{key} must be an integer, got {v!r}")
                return 0

        worker_id, tier = req("DWR_WORKER_ID"), req("DWR_TIER")
        url, token = req("DWR_CONTROLLER_URL").rstrip("/"), req("DWR_WORKER_TOKEN")
        if tier and tier not in ("edge", "fog"):
            errors.append(f"DWR_TIER must be edge or fog (cloud is reserved for V2), got {tier!r}")
        slots: list[list[int]] = []
        raw_slots = req("DWR_SLOT_CPUS")
        if raw_slots:
            try:
                slots = parse_slot_cpus(raw_slots)
                ncpu = psutil.cpu_count() or 1
                bad = [c for s in slots for c in s if not 0 <= c < ncpu]
                if bad:
                    errors.append(f"DWR_SLOT_CPUS uses cores {bad}, this host has 0..{ncpu - 1}")
            except ValueError as e:
                errors.append(f"DWR_SLOT_CPUS: {e}")
        budget, floor = integer("DWR_MEM_BUDGET_MB"), integer("DWR_MEM_FLOOR_MB")
        data_dir = Path(env.get("DWR_DATA_DIR", "./var/worker"))
        if errors:
            raise ConfigError("invalid worker configuration:\n  - " + "\n  - ".join(errors))
        return cls(worker_id, tier, url, token, slots, budget, floor, data_dir)


class Agent:
    def __init__(self, cfg: AgentConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.instance_id = uuid.uuid4().hex
        self.seq = 0
        self.wake = threading.Event()
        self.journal = Journal(cfg.data_dir / "journal.db")
        self.supervisor = Supervisor(self.journal, cfg.slot_cpus, cfg.data_dir / "jobs",
                                     on_terminal=self.wake.set, fetch_input=self.fetch_input)
        self.telemetry = TelemetrySampler()
        self.client = client or httpx.Client(
            base_url=f"{cfg.controller_url}/api/v1", timeout=REQUEST_TIMEOUT_S,
            headers={"Authorization": f"Bearer {cfg.token}"},
        )
        # Separate client: downloads run on supervisor threads, heartbeats on the main thread.
        self.download_client = httpx.Client(
            timeout=DOWNLOAD_TIMEOUT,
            headers={"Authorization": f"Bearer {cfg.token}", "Accept-Encoding": "identity"},
        )
        self.registered = False
        self.drain = False
        self.heartbeat_interval_s = 1.0
        self.rtt_ms_last: float | None = None

    # ------------------------------------------------------------ registration
    def registration_payload(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "worker_id": self.cfg.worker_id,
            "tier": self.cfg.tier,
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "os_version": platform.version() if platform.system() == "Windows" else platform.release(),
            "python": platform.python_version(),
            "cpu_logical": psutil.cpu_count() or 1,
            "mem_total_mb": psutil.virtual_memory().total // (1024 * 1024),
            "slots": [{"slot": i, "cpus": c} for i, c in enumerate(self.cfg.slot_cpus)],
            "mem_budget_mb": self.cfg.mem_budget_mb,
            "mem_floor_mb": self.cfg.mem_floor_mb,
            "supported_types": sorted(IMPLEMENTED),
            "capabilities": {"affinity": True, "cpu_quota": "not_enforced",
                             "rss_watchdog": True, "memory_hard_limit": "not_enforced"},
            "agent_version": AGENT_VERSION,
            "agent_instance_id": self.instance_id,
        }

    def register(self) -> None:
        r = self.client.post("/workers/register", json=self.registration_payload())
        self._raise_if_fatal(r)
        r.raise_for_status()
        reply = RegisterReply.model_validate(r.json())
        self.heartbeat_interval_s = reply.heartbeat_interval_s
        self.registered = True
        log.info("registered as %s (instance %s)", self.cfg.worker_id, self.instance_id)

    # ------------------------------------------------------------ input download
    def fetch_input(self, job_id: str, spec: dict, dest: Path, cancel: threading.Event) -> tuple[int, float]:
        """Stream the job's input from the controller to dest; verify size and sha256.
        transfer_s is measured on the monotonic clock from request to last byte."""
        url = f"{self.cfg.controller_url}{spec['input_url']}"
        h, n = hashlib.sha256(), 0
        t0 = time.perf_counter()
        with self.download_client.stream("GET", url) as r:
            if r.status_code != 200:
                raise RuntimeError(f"GET {spec['input_url']} -> HTTP {r.status_code}")
            with open(dest, "wb") as f:
                for chunk in r.iter_raw(DOWNLOAD_CHUNK):
                    if cancel.is_set():
                        raise TransferCancelled()
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
        transfer_s = time.perf_counter() - t0
        if n != spec["input_bytes"]:
            raise RuntimeError(f"received {n} bytes, expected {spec['input_bytes']}")
        if h.hexdigest() != spec["input_sha256"]:
            raise RuntimeError("input sha256 mismatch")
        return n, transfer_s

    # ------------------------------------------------------------ heartbeat
    def build_heartbeat(self) -> dict:
        self.seq += 1
        t = self.telemetry.sample(self.supervisor.job_processes())
        t["free_slots"] = self.supervisor.free_slots()
        t["rtt_ms_last"] = self.rtt_ms_last
        return {
            "schema_version": SCHEMA_VERSION,
            "agent_instance_id": self.instance_id,
            "seq": self.seq,
            "telemetry": t,
            "jobs": [row_to_snapshot(r) for r in self.journal.snapshot_rows()],
        }

    def apply_reply(self, reply: HeartbeatReply) -> None:
        """Write every assignment to the journal before anything else (plan §5.3 step 1)."""
        cancel = set(reply.cancel)
        for a in reply.assignments:
            err = self._validate(a)
            if self.journal.accept(a.job_id, a.model_dump(), cancelled=a.job_id in cancel, invalid_error=err):
                if err:
                    log.warning("rejected assignment %s: %s", a.job_id, err)
                    self.wake.set()
                elif a.job_id in cancel:
                    self.wake.set()
        for job_id in cancel:
            self.supervisor.request_cancel(job_id)
        self.journal.ack_terminal(reply.ack_terminal)
        self.drain = reply.drain

    @staticmethod
    def _validate(a: Assignment) -> str | None:
        if a.type not in IMPLEMENTED:
            return f"type {a.type!r} is not implemented on this worker"
        try:
            validate_assignment(a)
        except JobValidationError as e:
            return str(e)
        return None

    def heartbeat_once(self) -> bool:
        payload = self.build_heartbeat()
        t0 = time.perf_counter()
        try:
            r = self.client.post(f"/workers/{self.cfg.worker_id}/heartbeat", json=payload)
        except httpx.HTTPError as e:  # includes timeouts: a late reply is never applied
            log.warning("heartbeat seq=%s failed: %s", payload["seq"], e)
            return False
        self.rtt_ms_last = (time.perf_counter() - t0) * 1000
        self._raise_if_fatal(r)
        if r.status_code == 404:
            log.warning("controller does not know this worker; re-registering")
            self.registered = False
            return False
        if r.status_code != 200:
            log.warning("heartbeat seq=%s got HTTP %s: %s", payload["seq"], r.status_code, r.text[:300])
            return False
        self.apply_reply(HeartbeatReply.model_validate(r.json()))
        return True

    @staticmethod
    def _raise_if_fatal(r: httpx.Response) -> None:
        if r.status_code == 401:
            raise FatalAgentError("controller rejected the worker token (401); check DWR_WORKER_TOKEN "
                                  "and DWR_WORKER_TOKENS on the controller")
        if r.status_code == 409 and "schema" in r.text:
            raise FatalAgentError(f"controller refused this agent version: {r.text[:300]}")

    # ------------------------------------------------------------ main loop
    def run(self, stop: threading.Event) -> None:
        # Plan §5.5: before the first heartbeat, nothing of a previous instance may survive.
        recover(self.journal, self.supervisor.jobs_dir)
        sup = threading.Thread(target=self.supervisor.run, args=(stop,), name="supervisor", daemon=True)
        sup.start()
        last_prune = 0.0
        try:
            while not stop.is_set():
                if not self.registered:
                    try:
                        self.register()
                    except httpx.HTTPError as e:
                        log.warning("register failed (%s); retrying in %ss", e, REGISTER_RETRY_S)
                        stop.wait(REGISTER_RETRY_S)
                        continue
                started = time.monotonic()
                self.wake.clear()  # a terminal event during the request triggers another heartbeat
                self.heartbeat_once()
                if time.monotonic() - last_prune > PRUNE_EVERY_S:
                    self.journal.prune()
                    last_prune = time.monotonic()
                self.wake.wait(max(0.0, self.heartbeat_interval_s - (time.monotonic() - started)))
        finally:
            stop.set()
            sup.join(timeout=5)
            self.client.close()
            self.download_client.close()
            self.journal.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="worker.agent")
    ap.add_argument("--env-file", default=None, help='default: .env if present; "-" = environment only')
    ap.add_argument("--log-file", default=None,
                    help="append stdout/stderr here (start.ps1 uses it so no console handles are inherited)")
    args = ap.parse_args(argv)
    if args.log_file:
        sys.stdout = sys.stderr = open(args.log_file, "a", buffering=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # otherwise one line per heartbeat
    env_file = args.env_file if args.env_file is not None else (".env" if Path(".env").is_file() else "-")
    try:
        cfg = AgentConfig.from_env(load_env(env_file))
    except (ConfigError, FileNotFoundError, ValueError) as e:
        print(f"dwr-worker: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    agent = Agent(cfg)
    log.info("worker %s tier=%s slots=%s controller=%s", cfg.worker_id, cfg.tier, cfg.slot_cpus,
             cfg.controller_url)
    try:
        agent.run(stop)
    except FatalAgentError as e:
        log.error("%s", e)
        return 3
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
