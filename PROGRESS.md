# Progress

Branch: `v1`. Current milestone: **M5 code done**. M3, M4 and M5 hardware ACs are waiting
for the owner (runbooks: docs/evidence/m3.md, m4.md, m5.md). M1 and M2 passed on hardware
2026-11-15.

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

## M4 — CODE DONE, HARDWARE ACs NOT RUN
- [x] cancel: PENDING (controller), ACCEPTED (never spawned), RUNNING (verified kill)
- [x] timeout_s / max_runtime_s; RSS watchdog (tree RSS > 110% declared; base 48 MB, D-019)
- [x] verified termination; stuck slots (D-020)
- [x] staleness sweep → UNKNOWN → reconciliation; lost and queue timeouts (D-021)
- [x] duplicate seq / assignment (journal + `spawns` log); agent restart recovery
- [x] controller restart (state from SQLite; unheard workers go stale after STALE_S)
- [x] POST /jobs/{id}/cancel, POST /workers/{id}/drain|enable
- AC-4.5 PASS (local); AC-4.1–4.4, 4.6, 4.7 NOT RUN on hardware (reproduced locally)

## M5 — CODE DONE, HARDWARE AC NOT RUN
- [x] Workers, Jobs, Decisions, Overview, Submit views on real API data (frontend/)
- [x] same-origin API; controller serves frontend/dist; setup.ps1 builds it
- [x] GET /overview, GET /decisions, newest-first lists (D-022)
- [x] mid-term code moved to legacy/ with git mv (D-023)
- AC-5.1 PASS by grep (owner review pending); AC-5.2 PASS (local); AC-5.3 NOT RUN on hardware
- Tests: `.\.venv\Scripts\python -m pytest` → 250 passed; `npm run lint` clean

## Next step
Owner: copy the new code to the Linux laptop; run the m3, m4 and m5 runbooks (one session).
Then M6: gen_trace.py, replay.py, analyze.py, run tagging (POST /runs, export), reference
timing run, load sweep.

## Known issues
- Plan references PLAN_CRITIQUE.md, V1_CRITIQUE.md, PROJECT_PLAN_v2.md; not in the repo.
- Host-busy windows are in memory: a controller restart starts them empty (~5 s to re-detect).
- stop.ps1 hard-kills the agent: its running jobs end FAILED(agent_restart) at the next start
  (plan §5.5: jobs do not survive an agent restart).
- Blob cache is never pruned (one file per distinct input size).
- No systemd unit yet (Linux agent runs in a terminal; Ctrl+C there also kills its jobs).
- fog-1 (pop-os) has 2 cores / 3.7 GB RAM: one slot; with apps open, available < 512 MB
  floor and jobs wait (`mem_floor`). stress.py needs `--cpus all` there (D-018).
- Fog transfers over the phone hotspot are slow (50 MiB in ~22 s); record the network
  with every experiment.
- Linux copy was made by zip, not git: code changes need re-copying (keep its `.env`).
- One full test run (of ~10) in M2 had 4 timing-related e2e failures not reproduced since.
- `app/scripts/load_test.py` is untracked pre-existing work; left in app/scripts/ (not moved).
- README.md still describes the mid-term prototype (a pointer note was added).
