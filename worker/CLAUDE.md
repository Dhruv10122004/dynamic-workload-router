# worker/

The worker agent that runs on each device (Edge on Windows, Fog on Linux). Implements
plan §5: register, 1 s HTTP pull heartbeat, journal, supervisor, telemetry.

## Public interface
- `agent.py`: `python -m worker.agent [--env-file PATH|-]`. `AgentConfig.from_env(env)`
  (raises `ConfigError` listing every problem), `parse_slot_cpus("1,2;3")`,
  `Agent(cfg, client=None)` with `register()`, `build_heartbeat()`, `apply_reply(reply)`,
  `heartbeat_once()`, `run(stop_event)`.
- `journal.py`: `Journal(path)` — `accept()` (step 1, duplicate-safe), `mark_running()`
  (step 2), `mark_terminal()` (step 3), `ack_terminal()` (step 4), `prune()`,
  `snapshot_rows()`, `accepted_fifo()`; `row_to_snapshot(row)`.
- `supervisor.py`: `Supervisor(journal, slot_cpus, jobs_dir, on_terminal=)` — `tick()`,
  `run(stop)`, `request_cancel(job_id)`, `free_slots()`, `job_processes()`;
  `classify_exit(rc, job_dir)`.
- `telemetry.py`: `TelemetrySampler().sample(roots)`, `foreign_cpu_pct(host, own)`.

## Rules
- Never listen on a port; only call the controller.
- Write-ahead: commit each journal step before the next action (assignment journaled
  before spawn; PID journaled right after spawn).
- At most one heartbeat in flight; a reply to a failed/timed-out request is never applied.
- A job is a process tree (Windows venv launcher, D-006): measure CPU over the tree,
  kill the tree.
- Report durations only (monotonic clock); never wall-clock timestamps for job timing.
- M1 limits: running-job cancel, timers, RSS watchdog, restart recovery come in M4.

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_journal.py tests/unit/test_supervisor.py tests/unit/test_agent.py`
