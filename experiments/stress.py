"""Controlled foreign CPU load for the host-busy experiment (plan §7.2, §12.6, AC-3.4).

Starts one busy-loop process per chosen core, each pinned to its core and running a duty
cycle (busy for duty*period, idle for the rest), so the host's CPU% lands near a target.
DWR's agent counts this as *foreign* CPU: it is not one of its job processes.

    python -m experiments.stress --target-host-pct 80 --duration 120 [--cpus 0|all|0,2] [--delay 60]
    python -m experiments.stress --cpus all --follow-runs --block ablation --admin-token <token>

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


def burn(cpus: list[int], duty: float, duration: float, stop) -> float:
    """Load `cpus` at `duty` for `duration` s (0 = until stop). Returns the seconds it ran."""
    halt = mp.Event()
    procs = [mp.Process(target=_burn, args=(c, duty, halt), daemon=True) for c in cpus]
    t0 = time.monotonic()
    print(f"started at {time.strftime('%H:%M:%S')}", flush=True)
    for p in procs:
        p.start()
    psutil.cpu_percent()
    try:
        while not stop.is_set() and (not duration or time.monotonic() - t0 < duration):
            stop.wait(2.0)
            print(f"  t={time.monotonic() - t0:5.0f}s host CPU {psutil.cpu_percent():5.1f}%", flush=True)
    finally:
        halt.set()
        for p in procs:
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
        ran = time.monotonic() - t0
        print(f"stopped at {time.strftime('%H:%M:%S')} after {ran:.0f} s", flush=True)
    return ran


def follow_runs(url: str, token: str | None, block: str | None, cpus: list[int], duty: float, stop) -> None:
    """Ablation helper (plan §12.6): for every run the controller opens whose config asks for
    stress, load this host from +at_s to +at_s+for_s of that run. Timing uses the controller's
    `elapsed_s`, so the two machines' clocks need not agree. Runs until Ctrl+C."""
    import httpx

    headers = {"X-Admin-Token": token} if token else {}
    seen: set[str] = set()
    print(f"following runs on {url} (block={block or 'any'}); Ctrl+C to stop", flush=True)
    with httpx.Client(base_url=url.rstrip("/") + "/api/v1", headers=headers, timeout=10) as c:
        while not stop.is_set():
            try:
                runs = c.get("/runs", params={"active": "true"}).raise_for_status().json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise SystemExit("controller refused the admin token (401): pass --admin-token") from None
                print(f"controller error {e.response.status_code}; retrying", flush=True)
                stop.wait(5)
                continue
            except httpx.TransportError as e:
                print(f"controller unreachable ({type(e).__name__}); retrying", flush=True)
                stop.wait(5)
                continue
            for run in runs:
                cfg = run.get("config") or {}
                stress = cfg.get("stress")
                if run["run_id"] in seen or not stress or (block and cfg.get("block") != block):
                    continue
                seen.add(run["run_id"])
                wait = stress["at_s"] - (run.get("elapsed_s") or 0.0)
                print(f"run {run['run_id']} ({run['name']}): stress at +{stress['at_s']:.0f} s for "
                      f"{stress['for_s']:.0f} s; starting in {max(0.0, wait):.0f} s", flush=True)
                if wait > 0 and stop.wait(wait):
                    return
                burn(cpus, duty, stress["for_s"], stop)
            stop.wait(1.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Foreign CPU load for the host-busy experiment")
    ap.add_argument("--target-host-pct", type=float, default=80.0, help="whole-host CPU%% to aim for")
    ap.add_argument("--duration", type=float, default=0, help="seconds; 0 = until Ctrl+C")
    ap.add_argument("--delay", type=float, default=0, help="seconds to wait before starting")
    ap.add_argument("--cpus", help="'all', or comma list; default: cores not in DWR_SLOT_CPUS")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--follow-runs", action="store_true",
                    help="ablation: stress each run that asks for it (controller from DWR_CONTROLLER_URL)")
    ap.add_argument("--block", help="with --follow-runs: only runs of this block")
    ap.add_argument("--url", help="controller URL (default DWR_CONTROLLER_URL from .env)")
    ap.add_argument("--admin-token", help="needed from another machine (the controller's DWR_ADMIN_TOKEN)")
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

    stop = mp.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if a.follow_runs:
        url = a.url or env.get("DWR_CONTROLLER_URL")
        if not url:
            ap.error("--follow-runs needs --url or DWR_CONTROLLER_URL in .env")
        follow_runs(url, a.admin_token or env.get("DWR_ADMIN_TOKEN"), a.block, cpus, duty, stop)
        return 0
    if a.delay:
        print(f"waiting {a.delay:.0f} s before starting", flush=True)
        if stop.wait(a.delay):
            return 0
    burn(cpus, duty, a.duration, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
