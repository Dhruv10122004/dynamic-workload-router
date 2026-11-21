# experiments/

The evaluation harness (plan §12, M6) plus the M3 load/audit tools. It runs on the
controller laptop (stress.py runs on the worker being stressed).

## Public interface
Run from the repo root with the venv python: `python -m experiments.<name> --help`.
- `reference.py [--reps 8]`: reference timing run. Every job size (gen_trace.VARIANTS)
  N times under EdgeOnly → `experiments/reference.json` (per size on Edge: mean exec_s and
  mean service_s = assign→end slot time; Edge slots; power state). Only defines load levels.
- `gen_trace.py --load L --seed S --out t.csv [--mix mixed --arrival poisson|bursts]
  [--capacity-basis service|exec_s]`: seeded trace CSV + `.meta.json` (sha256).
  Rate = L × Edge slots / E[slot time] (AC-6.1; basis D-026, default service).
- `replay.py --trace t.csv --policy P [--host-busy on|off] --block B`: one tagged run:
  idle wait → PUT /scheduler → POST /runs → open-loop submit → drain → cancel leftovers →
  finish → `results/<block>/<run_id>/{manifest.json,jobs,decisions,telemetry,events.csv}`.
  `run_trace(c, ...)` is the library form.
- `sweep.py configs/<block>.json [--dry-run]`: whole block, unattended and resumable
  (`index.json`); same trace per (load, rep) for every strategy; strategy order rotates.
  Configs: `sweep.json` (3 strategies × 5 loads × 3 reps), `ablation.json` (host-busy off/on
  with stress), `pilot.json`.
- `analyze.py results/<block>`: from the run directories alone → `analysis/runs.csv`,
  `summary.csv`, `ablation.csv`, `report.md`, PNG plots (AC-6.2; definitions in D-025).
- `stress.py`: foreign CPU load (§12.6). `--follow-runs --block ablation --admin-token T`
  stresses each run that asks for it, timed by the controller's `elapsed_s` (D-024).
- `burst.py`, `audit.py` (M3): burst placement report; overbooking replay of job_events.
- `common.py`: `client()`, `git_commit()`, `wait_idle()`, `workers_snapshot()`, `percentile()`.

## Rules
- Talk to the controller only through its HTTP API (audit.py reads SQLite read-only).
- Never write to the controller DB. Never import controller internals.
- Reproducible: traces come only from (reference, load, mix, arrival, duration, seed);
  every run records git commit, config, trace hash, workers/slots, network, median RTT.
- analyze.py reads nothing but the results directory.
- Plotting needs `requirements-analysis.txt` (matplotlib), on the controller laptop only.
- Measure the reference and run the blocks in the same power state (mains power); every
  run records `harness_power` and analyze.py prints it.

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_experiments.py tests/integration/test_runs_api.py tests/integration/test_scheduling.py`
