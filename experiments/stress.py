"""Controlled foreign CPU load for the host-busy experiment (plan §7.2, §12.6, AC-3.4).

Starts one busy-loop process per chosen core, each pinned to its core and running a duty
cycle (busy for duty*period, idle for the rest), so the host's CPU% lands near a target.
DWR's agent counts this as *foreign* CPU: it is not one of its job processes.

    python -m experiments.stress --target-host-pct 80 --duration 120 [--cpus 0|all|0,2] [--delay 60]

Default cores: those NOT in DWR_SLOT_CPUS (from .env), as plan §12.6 says. On a host where
the free cores can't reach the target (e.g. 2 cores, 1 slot) the script says so; use
`--cpus all` then (the load then also competes with the job slots, which is still foreign load).
Stops at --duration or Ctrl+C, and always kills its children.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import signal
import sys
import time

import psutil

from shared.envfile import load_env
from worker.agent import parse_slot_cpus

PERIOD_S = 0.1


def _burn(core: int, duty: float, stop) -> None:
    try:
        psutil.Process().cpu_affinity([core])
    except (AttributeError, psutil.Error):
        pass  # macOS: no affinity; load still lands somewhere
    busy = PERIOD_S * duty
    try:
        while not stop.is_set():
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < busy:
                pass
            rest = PERIOD_S - (time.perf_counter() - t0)
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:  # Ctrl+C reaches the whole process group; the parent cleans up
        pass


def choose_cpus(spec: str | None, env: dict[str, str], ncpu: int) -> list[int]:
    if spec == "all":
        return list(range(ncpu))
    if spec:
        cpus = sorted({int(c) for c in spec.split(",") if c.strip()})
        bad = [c for c in cpus if not 0 <= c < ncpu]
        if bad:
            raise SystemExit(f"--cpus: cores {bad} do not exist (host has 0..{ncpu - 1})")
        return cpus
    slots = env.get("DWR_SLOT_CPUS", "").strip()
    used = {c for s in parse_slot_cpus(slots) for c in s} if slots else set()
    free = [c for c in range(ncpu) if c not in used]
    if not free:
        raise SystemExit("every core is a slot core; pass --cpus explicitly (e.g. --cpus all)")
    return free


def plan_duty(target_host_pct: float, n_cores: int, ncpu: int) -> tuple[float, float]:
    """(duty per core, host CPU% it should give)."""
    duty = min(1.0, target_host_pct / 100 * ncpu / n_cores)
    return duty, 100 * duty * n_cores / ncpu


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Foreign CPU load for the host-busy experiment")
    ap.add_argument("--target-host-pct", type=float, default=80.0, help="whole-host CPU%% to aim for")
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = until Ctrl+C")
    ap.add_argument("--delay", type=float, default=0, help="seconds to wait before starting")
    ap.add_argument("--cpus", help="'all', or comma list; default: cores not in DWR_SLOT_CPUS")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args(argv)
    if not 0 < a.target_host_pct <= 100:
        ap.error("--target-host-pct must be in (0, 100]")

    ncpu = psutil.cpu_count() or 1
    env = load_env(a.env_file if os.path.exists(a.env_file) else None)
    cpus = choose_cpus(a.cpus, env, ncpu)
    duty, expect = plan_duty(a.target_host_pct, len(cpus), ncpu)
    print(f"stress: cores {cpus} of {ncpu}, duty {duty:.0%} each -> about {expect:.0f}% host CPU")
    if expect + 0.5 < a.target_host_pct:
        print(f"warning: these cores can give at most {expect:.0f}% host CPU, below the target "
              f"{a.target_host_pct:.0f}% (and maybe below HOST_BUSY_HIGH). Use --cpus all.", flush=True)
    if a.delay:
        print(f"waiting {a.delay:.0f} s before starting", flush=True)
        time.sleep(a.delay)

    stop = mp.Event()
    procs = [mp.Process(target=_burn, args=(c, duty, stop), daemon=True) for c in cpus]
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    t0 = time.monotonic()
    print(f"started at {time.strftime('%H:%M:%S')}", flush=True)
    for p in procs:
        p.start()
    psutil.cpu_percent()
    try:
        while not stop.is_set() and (not a.duration or time.monotonic() - t0 < a.duration):
            stop.wait(2.0)
            print(f"  t={time.monotonic() - t0:5.0f}s host CPU {psutil.cpu_percent():5.1f}%", flush=True)
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
        print(f"stopped at {time.strftime('%H:%M:%S')} after {time.monotonic() - t0:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
