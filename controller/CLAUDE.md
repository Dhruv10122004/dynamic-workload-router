# controller/

The only process that listens (FastAPI, /api/v1). Owns the SQLite source of truth,
handles registration and heartbeats, accepts jobs, places them. Implements plan §6, §8, §9.

## Public interface
- Run: `python -m controller [--env-file PATH|-] [--log-file PATH]`; binds every
  `DWR_BIND_HOSTS` address; pins itself to `DWR_CONTROLLER_CPUS` (default: every core not
  in `DWR_SLOT_CPUS`). Serves `frontend/dist` at `/`, mounted after the API.
- `main.create_app(settings)` (tests use it with TestClient); `settings.Settings.from_env(env)`
  (raises `SettingsError` listing every problem).
- `db.Database(path)`: `.lock` (single writer), `.tx()` (BEGIN IMMEDIATE), `.one()/.all()`;
  `transition_job(c, job_id, dst, trigger, ts=, fail_reason=, kill_reason=, **fields)`, `add_event()`.
- `service.ControllerService`: registration, heartbeats, jobs, workers, scheduling, reliability,
  scheduler config, runs. Raises `NotFound/Conflict/Invalid` (404/409/422).
  - Scheduling (§6.3, §6.4): `schedule_pass` builds `scheduler.WorkerView`s, calls the active
    policy (also inline, under the held lock, in a heartbeat that freed a slot, so that reply
    carries the next assignment: D-026); one decision row per placement/reject plus one at the first attempt, which sets
    `overflow` (D-015); aged jobs hold workers (D-017). Host-busy per worker from heartbeats,
    `host_busy`/`host_free` events.
  - Reliability (§6.5, §6.6; D-021): `maintenance()` every 1 s: stale → UNKNOWN, lost and
    queue timeouts; each applied snapshot reconciles the worker's UNKNOWN jobs.
  - Runs (D-024): `create_run` (409 unless idle, one open run), `finish_run`, `list_runs`
    (open runs carry `elapsed_s`), `export_run`.
- `playground.Playground(service)` (D-027, attached as `app.state.playground`): presets
  (`SCENARIOS`), hands-on palette (`TASKS`), `start/stop` activities (tagged runs, block
  "playground"), `submit_task`, `explain(job_id)` ("Why here?" card), `narrate(after_ts)` (story
  feed; each line has `kind` and `worker_id`, D-030), `catalog()` also serves `HINTS` + `LEGEND`, `live(after_ts)` (lanes per worker/slot with live stats, queue, scoreboard, compare),
  `recover()` (closes an open playground run and cancels its jobs at startup). All
  teacher-facing wording lives here.
- `overview.compute(conn, now, run_id=None)` (D-022) and `runs.export_rows()/to_csv()`:
  read-only report helpers. `blobs.BlobStore(root).ensure(size)`: data_digest inputs.
- Endpoints (admin = localhost or `X-Admin-Token`): `GET /health` (public), `/catalog`,
  `/workers[/{id}]`, `POST /workers/{id}/drain|enable`, `POST|GET /jobs`, `GET /jobs/{id}`,
  `POST /jobs/{id}/cancel`, `GET|PUT /scheduler`, `GET /events`, `/overview`, `/decisions`,
  `POST /runs`, `GET /runs[/{id}]`, `POST /runs/{id}/finish`, `GET /runs/{id}/export?table=`,
  `GET /playground/scenarios|activity|live?after=|explain/{id}`, `POST /playground/activity|activity/stop|task`.
  Worker (bearer token): `POST /workers/register`, `POST /workers/{id}/heartbeat`,
  `GET /jobs/{id}/input` (assigned worker only; token checked before state).

## Rules
- Never connect to a worker. Replies are level-triggered (§6.1).
- Every DB write: `async with db.lock:` + `with db.tx():`; never `await` inside a tx.
- CPU work (blob generation) runs via `asyncio.to_thread`, never on the event loop.
- Every job state change via `transition_job()` (writes job_events in the same tx).
- Timestamps are controller wall clock at observation; reservations are derived from
  jobs in COMMITTED states, never counted.
- Placement logic lives in scheduler/ (pure); the controller builds views and persists the
  outcome. Active scheduler config in the `scheduler_config` table, .env defaults (D-016).
- A background task writes one telemetry_samples row per fresh worker every 5 s.
- Live per-job stats from snapshots stay in memory (`WorkerRuntime.job_stats`); the job's slot
  is stored (`jobs.slot`). New columns go in `db.MIGRATIONS` so old databases upgrade (D-028).
- The playground never schedules: its jobs go through `service.submit` like any other.

## Test
`.\.venv\Scripts\python -m pytest tests/integration` (M3 `test_scheduling.py`, M4 `test_reliability.py`
and `test_e2e_m4.py`, M5 `test_dashboard_api.py`, M6 `test_runs_api.py`, playground
`test_playground.py` + `tests/unit/test_playground_unit.py`)
