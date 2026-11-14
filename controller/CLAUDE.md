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
  `list_workers`, `get_worker`, `schedule_pass`, `is_fresh`. Raises `NotFound/Conflict/Invalid`.
- Endpoints now: `GET /health` (public), `GET /catalog`, `GET /workers[/{id}]`,
  `POST /jobs`, `GET /jobs`, `GET /jobs/{id}` (admin: localhost or `X-Admin-Token`);
  `POST /workers/register`, `POST /workers/{id}/heartbeat`, `GET /jobs/{id}/input`
  (worker bearer token; input only for the assigned worker, token checked before state).

## Rules
- Never connect to a worker. Replies are level-triggered (§6.1).
- Every DB write: `async with db.lock:` + `with db.tx():`; never `await` inside a tx.
- CPU work (blob generation) runs via `asyncio.to_thread`, never on the event loop.
- Every job state change via `transition_job()` (writes job_events in the same tx).
- Timestamps are controller wall clock at observation; reservations are derived from
  jobs in COMMITTED states, never counted.
- M1 placement is pinned-only (`placement=<worker_id>`, policy `pinned_m1`); M3 replaces
  `_pinned_decision` with scheduler/.

## Test
`.\.venv\Scripts\python -m pytest tests/integration`
