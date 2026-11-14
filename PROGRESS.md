# Progress

Branch: `v1`. Current milestone: **M1** (Windows controller → Linux worker → subprocess → result).
Code complete and tested locally; **waiting for the owner's hardware run** (plan: M1 must
pass before anything else continues).

## M0: Preserve the baseline — DONE
- [x] Tag `midterm-baseline` (b5da067), branch `v1`
- [x] pyproject.toml, requirements.txt (pinned), conftest.py
- [x] CLAUDE.md, .env.example, DECISIONS.md, PROGRESS.md
- AC-0.1 PASS, AC-0.2 PASS — docs/evidence/m0.md

## M1 — CODE DONE, HARDWARE ACs NOT RUN
- [x] shared/ (states, catalog, schemas, envfile) with tests (all 10×10 state pairs)
- [x] worker/workloads cpu_hash (pin, cancel sentinel, atomic result, exit codes)
- [x] journal (write-ahead, duplicate-safe) + minimal supervisor (N slots, spawn, reap, classify)
- [x] agent: register, 1 s heartbeat + immediate on terminal, worker-side validation
- [x] controller: SQLite (all §9 tables), register, heartbeat (§6.1), POST /jobs (pinned),
      GET /jobs[/{id}], GET /workers[/{id}], GET /catalog, GET /health
- [x] scripts/setup.sh
- [ ] network setup on the real machines (firewall, bind IP, curl from Linux) — owner
- AC-1.4 PASS; AC-1.1, 1.2, 1.3, 1.5 NOT RUN (runbook: docs/evidence/m1.md)
- Tests: `.\.venv\Scripts\python -m pytest` → 162 passed

## Next step
Owner runs the docs/evidence/m1.md runbook (Windows controller + Linux fog-1) and records
the results. Then M2: Edge agent with start.ps1/stop.ps1 (-Sim), data_digest + blob store,
mem_touch, multi-slot pinning, controller pinned away from Edge cores.

## Known issues
- Plan references PLAN_CRITIQUE.md, V1_CRITIQUE.md, PROJECT_PLAN_v2.md; none are in the repo.
- `placement=auto` returns 422 until the M3 scheduler (D-007).
- Cancelling a RUNNING job only logs a warning; termination and timers are M4.
- Agent restart with non-terminal journal rows is not recovered yet (M4, §5.5).
- Staleness → UNKNOWN sweep not implemented (M4); freshness is only used for placement.
- `app/scripts/load_test.py` is untracked pre-existing work; left untouched.
