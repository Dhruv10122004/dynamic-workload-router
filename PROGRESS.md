# Progress

Branch: `v1`. Current milestone: **M3 next**. M1 and M2 passed on hardware on 2026-11-15
(Windows controller + edge-1, Linux pop-os fog-1, over a phone hotspot).

## M0: Preserve the baseline — DONE
- AC-0.1 PASS, AC-0.2 PASS — docs/evidence/m0.md

## M1 — DONE
- [x] shared/ (states, catalog, schemas, envfile), all 10×10 state pairs tested
- [x] cpu_hash; journal; supervisor; agent register + heartbeat; controller core API
- [x] scripts/setup.sh (rebuilds a pip-less `.venv` left by a failed first run)
- [x] network setup on the real machines (phone hotspot, 2026-11-15)
- AC-1.1–1.5 PASS — docs/evidence/m1.md (AC-1.3: owner matched 12/12 PIDs in htop)

## M2 — DONE
- [x] start.ps1 / stop.ps1 with PID files, `-Sim` (fog-sim on this laptop); setup.ps1
- [x] data_digest: blob store (D-010), GET /jobs/{id}/input, threaded download with
      measured transfer, size+sha256 check (D-011)
- [x] mem_touch: hold in exec_s, cancel checked during hold (perf_counter deadline)
- [x] multiple slots with core pinning (proved via `cpus_affinity`); controller pinned
      away from slot cores (D-012)
- AC-2.1–2.4 PASS — docs/evidence/m2.md
- Tests: `.\.venv\Scripts\python -m pytest` → 176 passed

## Next step
M3: scheduler/ (eligibility, host-busy, policies), scheduler loop, PUT /scheduler,
overflow flag, stress.py.

## Known issues
- Plan references PLAN_CRITIQUE.md, V1_CRITIQUE.md, PROJECT_PLAN_v2.md; not in the repo.
- `placement=auto` returns 422 until M3 (D-007).
- Cancel of a RUNNING job only logs a warning; timers, watchdog, restart recovery,
  staleness → UNKNOWN are M4. stop.ps1 hard-kills: in-flight jobs are lost.
- Blob cache is never pruned (one file per distinct input size).
- No systemd unit yet (Linux agent runs in a terminal).
- fog-1 (pop-os) has 2 cores / 3.7 GB RAM: with apps open, available < 512 MB floor and
  jobs wait in PENDING (`mem_floor`). Close apps before experiments.
- Fog transfers over the phone hotspot are slow (50 MiB in ~22 s); the network used for
  experiments must be recorded with the results.
- Linux copy was made by zip, not git: code changes need re-copying (keep its `.env`).
- One full test run (of ~10) had 4 timing-related e2e failures not reproduced since.
- `app/scripts/load_test.py` is untracked pre-existing work; left untouched.
