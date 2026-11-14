# Progress

Branch: `v1`. Current milestone: **M1** (Windows controller → Linux worker → subprocess → result).

## M0: Preserve the baseline — DONE
- [x] Tag `midterm-baseline` (b5da067), branch `v1`
- [x] pyproject.toml, requirements.txt (pinned), conftest.py
- [x] CLAUDE.md, .env.example, DECISIONS.md, PROGRESS.md
- AC-0.1 PASS, AC-0.2 PASS — docs/evidence/m0.md

## M1 — IN PROGRESS
- [ ] shared/ (states, catalog, schemas) with tests
- [ ] worker/workloads cpu_hash
- [ ] journal + minimal supervisor
- [ ] agent register + heartbeat
- [ ] controller: SQLite, register, heartbeat, POST /jobs (placement=<worker_id>), GET /jobs/{id}
- [ ] scripts/setup.sh
- [ ] network setup (firewall rule, bind IP, curl from Linux) — needs owner on hardware

## Next step
Implement M1 code and tests.

## Known issues
- Plan references PLAN_CRITIQUE.md, V1_CRITIQUE.md, PROJECT_PLAN_v2.md; none are in the repo.
