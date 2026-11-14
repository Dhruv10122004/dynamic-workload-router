# Progress

Branch: `v1`. Current milestone: **M2 code done**; M1 + M2 hardware ACs to be run together
by the owner (runbooks: docs/evidence/m1.md, m2.md). Plan: M1 must pass before more work.

## M0: Preserve the baseline — DONE
- AC-0.1 PASS, AC-0.2 PASS — docs/evidence/m0.md

## M1 — CODE DONE, HARDWARE ACs NOT RUN
- [x] shared/ (states, catalog, schemas, envfile), all 10×10 state pairs tested
- [x] cpu_hash; journal; supervisor; agent register + heartbeat; controller core API
- [x] scripts/setup.sh
- [ ] network setup on the real machines — owner
- AC-1.4 PASS; AC-1.1, 1.2, 1.3, 1.5 NOT RUN

## M2 — CODE DONE, HARDWARE ACs NOT RUN
- [x] start.ps1 / stop.ps1 with PID files, `-Sim` (fog-sim on this laptop); setup.ps1
- [x] data_digest: blob store (D-010), GET /jobs/{id}/input, threaded download with
      measured transfer, size+sha256 check (D-011)
- [x] mem_touch: hold in exec_s, cancel checked during hold (perf_counter deadline)
- [x] multiple slots with core pinning (proved via `cpus_affinity`); controller pinned
      away from slot cores (D-012)
- AC-2.1–2.4 NOT RUN on hardware; all reproduced locally with fog-sim (docs/evidence/m2.md)
- Tests: `.\.venv\Scripts\python -m pytest` → 176 passed

## Next step
Owner: run the M1 then M2 runbook on Windows + Linux and record results. Then M3:
scheduler/ (eligibility, host-busy, policies), scheduler loop, PUT /scheduler, overflow
flag, stress.py.

## Known issues
- Plan references PLAN_CRITIQUE.md, V1_CRITIQUE.md, PROJECT_PLAN_v2.md; not in the repo.
- `placement=auto` returns 422 until M3 (D-007).
- Cancel of a RUNNING job only logs a warning; timers, watchdog, restart recovery,
  staleness → UNKNOWN are M4. stop.ps1 hard-kills: in-flight jobs are lost.
- Blob cache is never pruned (one file per distinct input size).
- No systemd unit yet (Linux agent runs in a terminal).
- One full test run (of ~10) had 4 timing-related e2e failures not reproduced since.
- `app/scripts/load_test.py` is untracked pre-existing work; left untouched.
