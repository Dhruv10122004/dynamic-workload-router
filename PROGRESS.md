# Progress

Branch: `v1`. Current milestone: **M3 code done**; AC-3.2 and AC-3.4 to be run on the real
machines by the owner (runbook: docs/evidence/m3.md). M1 and M2 passed on hardware 2026-11-15.

## M0: Preserve the baseline — DONE
- AC-0.1 PASS, AC-0.2 PASS — docs/evidence/m0.md

## M1 — DONE
- AC-1.1–1.5 PASS — docs/evidence/m1.md (AC-1.3: owner matched 12/12 PIDs in htop)

## M2 — DONE
- AC-2.1–2.4 PASS — docs/evidence/m2.md

## M3 — CODE DONE, HARDWARE ACs NOT RUN
- [x] scheduler/ (views, eligibility, host-busy, policies) with exhaustive unit tests
- [x] scheduler loop: aged first + FIFO with skip, aged jobs hold workers (D-017),
      decision rows, overflow flag at first attempt (D-015)
- [x] derived reservations under the single writer; structural reject rules (D-014)
- [x] GET/PUT /scheduler (409 while busy, persisted; D-016), GET /events, telemetry samples
- [x] experiments/stress.py (D-018), burst.py, audit.py
- AC-3.1, 3.3, 3.5 PASS; AC-3.2, 3.4 NOT RUN on hardware (reproduced locally)
- Tests: `.\.venv\Scripts\python -m pytest` → 219 passed

## Next step
Owner: copy the new code to the Linux laptop, run the m3 runbook. Then M4: cancel/kill,
timers, watchdog, agent + controller restart recovery, staleness → UNKNOWN → lost,
queue timeout.

## Known issues
- Plan references PLAN_CRITIQUE.md, V1_CRITIQUE.md, PROJECT_PLAN_v2.md; not in the repo.
- Cancel of a RUNNING job only logs a warning; timers, watchdog, restart recovery,
  staleness → UNKNOWN, queue_timeout are M4. Jobs that can never be placed (e.g. EdgeOnly
  with Edge down) wait forever until M4's queue timeout. stop.ps1 hard-kills: in-flight
  jobs are lost.
- Host-busy windows are in memory: a controller restart starts them empty (~5 s to re-detect).
- Blob cache is never pruned (one file per distinct input size).
- No systemd unit yet (Linux agent runs in a terminal).
- fog-1 (pop-os) has 2 cores / 3.7 GB RAM: one slot; with apps open, available < 512 MB
  floor and jobs wait (`mem_floor`). stress.py needs `--cpus all` there (D-018).
- Fog transfers over the phone hotspot are slow (50 MiB in ~22 s); record the network
  with every experiment.
- Linux copy was made by zip, not git: code changes need re-copying (keep its `.env`).
- One full test run (of ~10) in M2 had 4 timing-related e2e failures not reproduced since.
- `app/scripts/load_test.py` is untracked pre-existing work; left untouched.
