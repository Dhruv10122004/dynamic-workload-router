# experiments/

Tools for the hardware ACs and for evaluation (plan §12). M3 adds load and audit tools.
Coming in M6: `gen_trace.py`, `replay.py`, `analyze.py`, `configs/*.json`.

## Public interface
All are run from the repo root with the venv python: `python -m experiments.<name> --help`.
- `stress.py`: foreign CPU load for the host-busy experiment (§12.6, AC-3.4).
  `--target-host-pct 80 --duration 120 [--cpus all|0,2] [--delay 60]`. Default cores: those
  not in `DWR_SLOT_CPUS` (.env); warns when they can't reach the target (D-018). Runs on
  the worker host; Ctrl+C stops it and its children.
- `burst.py`: submits `--n` jobs at once (`--mix cpu|mem|mixed`, placement auto) to
  `--url`, waits for them, and prints placement per worker, overflow count and ineligible
  reasons (AC-3.2). Admin token from `--admin-token` or `DWR_ADMIN_TOKEN`.
- `audit.py`: `audit(db_path, run_id=None) -> {worker_id: WorkerPeak}`. Replays
  job_events in commit order and reports peak slots/memory and any overbooking (AC-3.3).
  CLI `--db var/controller/dwr.db [--run-id]`; exit code 1 on a violation. Opens the DB
  read-only, so it is safe while the controller runs.

## Rules
- Talk to the controller only through its HTTP API. The exception is audit.py, which
  reads SQLite read-only.
- Never write to the controller DB. Never import controller internals.
- Seeds and inputs must be reproducible. Every result records its config (plan §12.3).

## Test
`.\.venv\Scripts\python -m pytest tests/integration/test_scheduling.py` (uses `audit`);
the scripts themselves are exercised in the m3 evidence runs.
