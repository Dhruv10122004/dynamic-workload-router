# Design decisions

Decisions not already dictated by PROJECT_PLAN_V1.md. Never rewrite an entry;
supersede it with a new one.

### D-001 (2026-11-14, M0) Python 3.11 venv and pinned versions
Context: §3 requires Python 3.10–3.12 and pinned dependencies; the Windows laptop
has Python 3.11 and an MSYS 3.12 without packages on the Git Bash PATH.
Decision: project venv `.venv` from `py -3.11`; pin the versions already proven on this
machine (fastapi 0.115.12, starlette 0.46.2, uvicorn 0.34.2, pydantic 2.13.3, httpx 0.28.1,
psutil 7.2.2, pytest 9.1.1) in requirements.txt and pyproject.toml. No uvicorn[standard]
extras (fewer native wheels on the old Linux laptop).
Alternatives rejected: unpinned ranges (legacy app/requirements.txt style); poetry/uv
(extra tooling on two machines). Plan §: 3, M0.

### D-002 (2026-11-14, M0) Running the legacy tests from the repo root
Context: AC-0.1; legacy tests are named `tests_telemetry.py` and do `from main import app`.
Decision: pytest `python_files = test_*.py, tests_*.py`, `testpaths = tests, app/Tests`;
root conftest.py appends (not prepends) `app/` to sys.path so it cannot shadow V1 packages.
Alternatives rejected: renaming/editing legacy files (forbidden by AC-0.2). Plan §: M0.

### D-003 (2026-11-14, M0) Runtime data lives under var/
Context: controller DB, worker journal and job dirs must never be committed, but the root
.gitignore belongs to the frozen mid-term baseline.
Decision: all runtime data defaults to `./var/...`; a committed `var/.gitignore` ignores
everything else in it. Plan §: 5.1 (`DWR_DATA_DIR=./var/worker`), 16.1.

### D-004 (2026-11-14, M0/M1) Controller .env keys
Context: §5.1 defines worker keys; §8 says worker tokens are bound to worker_ids "in the
controller's .env" and the controller binds 127.0.0.1 plus a LAN/Tailscale IP, without
naming keys.
Decision: `DWR_WORKER_TOKENS=id:token,id:token`, `DWR_BIND_HOSTS=127.0.0.1,<ip>` (one
socket per host, served by one uvicorn server), `DWR_PORT`, `DWR_ADMIN_TOKEN`,
`DWR_CONTROLLER_DATA_DIR`, `DWR_TEST_MODE`, `DWR_STALE_S`. Controller and Edge agent may
share one `.env`; the agent also takes `--env-file` for running several agents on one host.
Alternatives rejected: a separate controller config file format (one more thing to set up).
Plan §: 3, 5.1, 8.

### D-005 (2026-11-14, M1) Where `transition()` lives
Context: §6.2 says the table lives in shared/states.py and "every change goes through
transition(), which writes a job_events row", but shared/ is used by the worker too and
must not touch the controller DB.
Decision: shared/states.py `transition()` is pure (validates src→dst and the reason,
returns dst). controller/db.py `transition_job()` is the only persisting path: it calls
`transition()` and writes the jobs update + job_events row in one transaction. FAILED
reasons are checked per source state: PENDING only `queue_timeout`; `lost` only from
UNKNOWN; other sources take worker-reported reasons. The worker journal uses
`can_transition()` for its own rows.
Alternatives rejected: passing a DB handle into shared/ (breaks purity). Plan §: 6.2.

### D-006 (2026-11-14, M1) A job is a process tree
Context: on Windows, a venv's `python.exe` is a launcher that starts the real interpreter
as a child process (verified on the Windows laptop: launcher PID 7436 → child 716).
Decision: the journal and controller record the root (Popen) PID and its create_time;
result.json also records the interpreter's own `pid`. Own-CPU telemetry sums over the
tree; termination (M4) kills the tree. On Linux the tree is a single process.
Alternatives rejected: spawning the base interpreter directly (loses venv packages);
guessing the child PID after spawn (racy). Plan §: 4, 5.4, 5.5.

### D-007 (2026-11-14, M1) Pinned-only placement until M3
Context: M1 needs `POST /jobs` with `placement=<worker_id>`; the scheduler is M3.
Decision: `placement=auto` and unknown worker ids get 422 until M3. A temporary
`_pinned_decision` applies the §7.1 checks to the named worker: if it can never run the
job (unsupported type, mem > budget) → REJECTED with a decision row (policy `pinned_m1`);
otherwise the job stays PENDING until a slot is free. No decision row is written for a
pass that leaves a job PENDING (avoids one row per second per waiting job).
Alternatives rejected: building scheduler/ early (M3 scope). Plan §: 6.2, 6.3, 7.1, M1.

### D-008 (2026-11-14, M1) GET /health is unauthenticated
Context: M1 checks connectivity from Linux with `curl /api/v1/health`; admin endpoints
need localhost or X-Admin-Token.
Decision: /health is public and returns only status, reasons and fresh worker ids.
Everything else under /api/v1 needs admin or worker auth. Plan §: 8, M1.

### D-009 (2026-11-14, M1) Worker validation at receipt; agent threading
Context: §5.3 step 1 inserts ACCEPTED; AC-1.4 needs FAILED(worker_validation).
Decision: the agent validates each assignment (catalog bounds, declared mem, runtime cap,
type implemented) before journaling; an invalid one is journaled directly as
FAILED(worker_validation) and never spawned. The worker accepts test-only params (the
controller gates them with DWR_TEST_MODE). Threads: heartbeat loop on the main thread,
supervisor on its own thread (100 ms tick), sharing the journal behind a lock; a terminal
job sets an event that triggers an immediate heartbeat. Journal adds `accepted_at`
(FIFO order) and `reported_at` (24 h prune) columns.
Alternatives rejected: asyncio agent (subprocess + psutil calls are blocking anyway).
Plan §: 5.2, 5.3.
