## Project documentation rules (mandatory)

### Per-folder docs
- Every source folder (shared/, scheduler/, controller/, worker/,
  worker/workloads/, experiments/, frontend/, scripts/) has its own CLAUDE.md.
- Create it when you create the folder. Keep it under ~60 lines. Contents:
  1. Purpose of this folder (2–3 lines) and which plan § it implements
  2. Public interface: key modules, classes, functions, endpoints, and how to call them
  3. Rules specific to this folder (e.g. "scheduler/ is pure: no I/O")
  4. How to test it (exact command)
- When you change a folder's interface or rules, update its CLAUDE.md
  in the SAME commit.

### Root docs
- PROJECT_PLAN_V1.md is the plan and definition of done (the ACs). Do not
  duplicate it. Never edit it without asking me.
- DECISIONS.md: one entry per real design decision not already dictated by the
  plan (library choice, schema detail, interpretation of an ambiguous section).
  Format:
    ### D-012 (2026-11-20, M3) Title
    Context: … Decision: … Alternatives rejected: … Plan §: …
  Never rewrite old entries; supersede them with a new one ("Supersedes D-007").
- PROGRESS.md: current milestone, checklist items done/in progress/blocked,
  next step, and known issues. Update it at the end of every task, before
  committing. Keep it under ~80 lines; detailed proof goes in docs/evidence/mN.md.
- An AC is PASS only with evidence. Hardware-dependent ACs stay NOT RUN until
  I verify them.

### Start of every session
Read PROGRESS.md first, then the plan section for the current milestone.

## Project quick reference

Current milestone: see PROGRESS.md. Work happens on branch `v1`; the mid-term
code (app/, router/, run_demo.py, scripts/load_generator.py, docker files) is
frozen at tag `midterm-baseline` and must not be modified until M5 moves it to
legacy/ with `git mv`.

### Invariants (plan §3) — never break these
1. Only the controller listens. The controller never connects to a worker.
2. Controller clock is the authority for submit/assign/start/end timestamps.
   Workers report durations only (exec_s, transfer_s, spawn_s), monotonic clock.
3. Jobs are catalog type + bounded params, validated on controller AND worker.
4. Each job is its own subprocess. Nothing CPU-heavy in an event loop.
5. SQLite is the source of truth on both sides; in-memory state is a cache.
6. Payload data originates on the controller host.
7. At-most-once execution, no automatic retry. Never claim exactly-once.
Every job state change goes through shared/states.py `transition()`.

### Commands (Windows, from repo root)
- Setup: `py -3.11 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt`
- Tests: `.\.venv\Scripts\python -m pytest` (unit + integration + legacy)
- Unit only: `.\.venv\Scripts\python -m pytest tests/unit`
- Controller: `.\.venv\Scripts\python -m controller` (reads .env)
- Worker agent: `.\.venv\Scripts\python -m worker.agent [--env-file path]`
- Linux worker: `./scripts/setup.sh`, then `.venv/bin/python -m worker.agent`
Note: in Git Bash `python` is an MSYS build without deps; use the venv python.