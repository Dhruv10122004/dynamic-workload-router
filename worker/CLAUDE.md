# worker/

The worker agent that runs on each device (Edge on Windows, Fog on Linux). Implements
plan §5: register, 1 s HTTP pull heartbeat, journal, supervisor (timers, RSS watchdog,
verified termination), restart recovery, telemetry.

## Public interface
- `agent.py`: `python -m worker.agent [--env-file PATH|-]`. `AgentConfig.from_env(env)`
  (raises `ConfigError` listing every problem), `parse_slot_cpus("1,2;3")`,
  `Agent(cfg, client=None)` with `register()`, `build_heartbeat()`, `apply_reply(reply)`,
  `heartbeat_once()`, `run(stop_event)` (runs `recover()` before the first heartbeat).
- `journal.py`: `Journal(path)` — `accept()` (step 1, duplicate-safe), `mark_running()`
  (step 2), `mark_terminal()` (step 3), `ack_terminal()` (step 4), `prune()`,
  `snapshot_rows()`, `accepted_fifo()`, `nonterminal_rows()`, `spawn_counts()` (from the
  `spawns` table, one row per spawn: AC-4.5); `row_to_snapshot(row)`.
- `supervisor.py`: `Supervisor(journal, slot_cpus, jobs_dir, on_terminal=, fetch_input=)` —
  `tick()`, `run(stop)`, `request_cancel(job_id)` (ACCEPTED → CANCELLED; RUNNING → verified
  KILLED(cancel)), `free_slots()`, `stuck_slots()`, `job_processes()`, `job_stats()` (live
  tree CPU% and RSS per running job, sampled by the watchdog; sent in RUNNING snapshots, D-028);
  `classify_exit(rc, job_dir)`.
  A slot is held during the input download (own thread), then by the process, then (if a
  kill was not verified) as `stuck` until the processes vanish. Each tick checks
  timeout_s / max_runtime_s from spawn and the tree's RSS every 250 ms vs 110% of `mem_mb`,
  and advances terminations (sentinel → 2 s → kill tree → verify ≤ 5 s; D-020).
  `fetch_input(job_id, spec, dest, cancel) -> (bytes, seconds)`.
- `recovery.py`: `recover(journal, jobs_dir) -> RecoveryReport` (plan §5.5): kills leftover
  workload processes of this jobs dir (verified), ends non-terminal rows as the real result
  or FAILED(agent_restart).
- `proctree.py`: `tree(root)`, `alive(p)` (pid + create_time), `kill(procs)`,
  `wait_gone(procs)`, `kill_verified(procs)` (blocking: recovery only),
  `find_job_processes(jobs_dir)`.
- `agent.Agent.fetch_input`: streams `input_url` from the controller (separate httpx
  client), verifies size + sha256, returns `transfer_bytes`, `transfer_s`.
- `telemetry.py`: `TelemetrySampler().sample(roots)` (own CPU from CPU-time deltas, D-029),
  `foreign_cpu_pct(host, own)`.

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
- Never block the supervisor tick on a kill: termination advances one step per tick.
- Recovery only touches processes whose `--job-dir` is in this agent's jobs dir (D-020).
- Never spawn a job twice: the journal row and the `spawns` row are one transaction.

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_telemetry.py tests/unit/test_journal.py tests/unit/test_supervisor.py tests/unit/test_supervisor_m4.py tests/unit/test_agent.py tests/unit/test_workloads_m2.py`
