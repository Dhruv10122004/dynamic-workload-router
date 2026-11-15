# controller/

The only process that listens (FastAPI, /api/v1). Owns the SQLite source of truth,
handles registration and heartbeats, accepts jobs, places them. Implements plan §6, §8, §9.

## Public interface
- Run: `python -m controller [--env-file PATH|-] [--log-file PATH]`; binds every
  `DWR_BIND_HOSTS` address; pins itself to `DWR_CONTROLLER_CPUS` (default: every core not
  in `DWR_SLOT_CPUS`), reported as `controller_cpus` by /health.
- `blobs.BlobStore(root).ensure(size) -> sha256`: deterministic data_digest inputs
  (SHAKE-256 of a fixed seed), one `<size>.bin` per size, reused. Call off the event loop.
- `main.create_app(settings) -> FastAPI` (tests use it with TestClient).
- `settings.Settings.from_env(env)` (raises `SettingsError` listing every problem).
- `db.Database(path)`: `.lock` (asyncio single-writer lock), `.tx()` (BEGIN IMMEDIATE),
  `.one()/.all()`; `transition_job(c, job_id, dst, trigger, ts=, fail_reason=, kill_reason=, **fields)`,
  `add_event(c, kind, ...)`.
- `service.ControllerService`: `register`, `heartbeat`, `submit`, `get_job`, `list_jobs`,
  `list_workers`, `get_worker`, `schedule_pass`, `is_fresh`, `get_scheduler`,
  `put_scheduler`, `list_events`, `sample_telemetry`, `maintenance`, `cancel`,
  `set_draining`. Raises `NotFound/Conflict/Invalid`.
- Reliability (plan §6.5, §6.6; D-021): `maintenance()` runs every 1 s. Stale workers get a
  `worker_stale` event and their committed jobs go to UNKNOWN. UNKNOWN longer than
  `DWR_LOST_TIMEOUT_S` → FAILED(lost); PENDING longer than `queue_timeout_s` →
  FAILED(queue_timeout). Each applied snapshot reconciles the worker's UNKNOWN jobs: a listed
  job takes its reported state; a missing one goes back to ASSIGNED if never acked, or to
  FAILED(lost) if acked.
- Scheduling (plan §6.3, §6.4): `schedule_pass` builds `scheduler.WorkerView`s from SQLite
  plus heartbeat state, then calls the active policy. It runs on submit, on a terminal
  snapshot, when a worker becomes fresh or not busy, and every 1 s. It writes one decision
  row per placement or reject, plus one at the first attempt; the first-attempt row sets
  `overflow` (D-015). Aged jobs hold workers (D-017).
- Host-busy: one `scheduler.HostBusy` per worker, fed `foreign_cpu_pct` from each applied
  heartbeat. A state change logs a `host_busy`/`host_free` event. `GET /workers` shows
  `host_busy` and `foreign_cpu_mean`.
- Endpoints now: `GET /health` (public), `GET /catalog`, `GET /workers[/{id}]`,
  `POST /jobs`, `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` (202 = requested;
  409 if terminal), `POST /workers/{id}/drain|enable`, `GET|PUT /scheduler` (PUT → 409
  while any job is non-terminal), `GET /events?after=&kind=&limit=` (admin: localhost or
  `X-Admin-Token`);
  `POST /workers/register`, `POST /workers/{id}/heartbeat`, `GET /jobs/{id}/input`
  (worker bearer token; input only for the assigned worker, token checked before state).

## Rules
- Never connect to a worker. Replies are level-triggered (§6.1).
- Every DB write: `async with db.lock:` + `with db.tx():`; never `await` inside a tx.
- CPU work (blob generation) runs via `asyncio.to_thread`, never on the event loop.
- Every job state change via `transition_job()` (writes job_events in the same tx).
- Timestamps are controller wall clock at observation; reservations are derived from
  jobs in COMMITTED states, never counted.
- Placement logic lives in scheduler/ (pure). The controller only builds the views and
  persists the outcome. The active scheduler config is stored in the `scheduler_config`
  table, with .env defaults (D-016).
- A background task writes one telemetry_samples row per fresh worker every 5 s.

## Test
`.\.venv\Scripts\python -m pytest tests/integration` (M3: `test_scheduling.py`; M4: `test_reliability.py`, `test_e2e_m4.py`)
