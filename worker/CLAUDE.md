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
- `supervisor.py`: `Supervisor(journal, slot_cpus, jobs_dir, on_terminal=, fetch_input=)` —
  `tick()`, `run(stop)`, `request_cancel(job_id)`, `free_slots()`, `job_processes()`;
  `classify_exit(rc, job_dir)`. A slot is held during the input download (own thread),
  then by the process. `fetch_input(job_id, spec, dest, cancel) -> (bytes, seconds)`.
- `agent.Agent.fetch_input`: streams `input_url` from the controller (separate httpx
  client), verifies size + sha256, returns `transfer_bytes`, `transfer_s`.
- `telemetry.py`: `TelemetrySampler().sample(roots)`, `foreign_cpu_pct(host, own)`.

## Rules
- Never listen on a port; only call the controller.
- Write-ahead: commit each journal step before the next action (assignment journaled
  before spawn; PID journaled right after spawn).
- At most one heartbeat in flight; a reply to a failed/timed-out request is never applied.
- A job is a process tree (Windows venv launcher, D-006): measure CPU over the tree,
  kill the tree.
- Report durations only (monotonic clock); never wall-clock timestamps for job timing.
- Never block the supervisor tick on the network (downloads run on their own thread).
- `python -m worker.agent --log-file PATH` writes its own log (start.ps1 uses it, D-013).
- Not yet: running-job cancel, timers, RSS watchdog, restart recovery (M4).

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_journal.py tests/unit/test_supervisor.py tests/unit/test_agent.py tests/unit/test_workloads_m2.py`
