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

### D-010 (2026-11-14, M2) data_digest inputs are a pure function of their size
Context: §11 asks for "deterministic payload generation (seeded)"; every strategy must
see identical inputs, and the catalog has no seed parameter.
Decision: input = SHAKE-256(`dwr-blob-v1`) truncated to `input_bytes`, generated once per
size (off the event loop), stored as `var/controller/blobs/<size>.bin` + `.sha256`, reused.
The job row stores `input_sha256`; the worker verifies size and hash after download.
Consequence: one cached file per distinct size, with no pruning yet; M6 traces should
use a small set of sizes.
Alternatives rejected: per-job random blobs (non-reproducible, unbounded disk); a seed
parameter (catalog change for no measurement benefit). Plan §: 3 inv. 6, 4, 11.

### D-011 (2026-11-14, M2) Edge downloads its input over localhost HTTP too
Context: invariant 6 says "Edge reads it locally; Fog downloads it"; AC-2.3 says "On
Edge, the transfer happens over localhost".
Decision: both tiers use the same path: GET `/api/v1/jobs/{id}/input`. For Edge,
DWR_CONTROLLER_URL is 127.0.0.1, so "local" means loopback, and transfer_s is measured
the same way on both tiers. The download runs on its own thread while holding the slot;
transfer_s = request start to last byte (monotonic). Failures → FAILED(input_transfer_error).
Alternatives rejected: Edge reading the blob file directly (two code paths; Edge
transfer_s would not be comparable). Plan §: 3, 5.3 step 2, AC-2.3.

### D-012 (2026-11-14, M2) Controller pinning and start.ps1 -Sim
Context: M2 says "controller pinned away from the Edge slot cores" and start.ps1 has a
`-Sim` mode, neither further specified.
Decision: the controller pins itself to `DWR_CONTROLLER_CPUS`, defaulting to every core
not in `DWR_SLOT_CPUS` (start.ps1 also excludes the sim cores). `-Sim` starts a second
agent `fog-sim` (tier fog) on this laptop with a per-session token, on
`DWR_SIM_SLOT_CPUS` (default: the two highest free cores), so Edge→Fog can be run
without the Linux laptop. Sim results are never evidence for Fog ACs.
Alternatives rejected: -Sim as a dry run that starts nothing. Plan §: 2, M2.

### D-013 (2026-11-14, M2) Background processes log to files themselves
Context: `Start-Process -Redirect*` creates children with handle inheritance, so the
Python processes held the caller's stdout pipe and `start.ps1 | Out-Null` never
returned (reproduced on the Windows laptop).
Decision: start.ps1 launches without redirection (no handle inheritance) and passes
`--log-file var\logs\<name>.log`; controller and agent point stdout/stderr at that file.
httpx request logging is set to WARNING in the agent (one line per heartbeat otherwise).
Plan §: 2 (Install), 13 M2.

### D-014 (2026-11-15, M3) Structural reject: who counts, which reasons. Supersedes D-007
Context: §6.2 rejects a job when "no registered worker supports the type, or the memory
exceeds every worker's budget"; M1 had pinned-only placement (D-007).
Decision: `placement=auto` is accepted. A worker "could ever run" a job if the job is not
pinned elsewhere, the type is supported and `mem_mb <= mem_budget_mb` (staleness, load,
draining and host-busy are transient). REJECTED when no registered worker could ever run
it; the decision then lists each worker's *permanent* reason (`not_requested`,
`unsupported_type`, `mem_budget`), not a transient one like `stale`. So a job pinned to a
worker that can't hold it is rejected (as in D-007). With no worker registered at all the
job waits. Policy restrictions (EdgeOnly/FogOnly) are not structural: such jobs wait and
count as overflow, which is the counterfactual Cloud demand §12.1 wants.
Alternatives rejected: rejecting on the first failing transient check. Plan §: 6.2, 7.1, 7.3.

### D-015 (2026-11-15, M3) Decision rows and the overflow flag
Context: §9 has a decisions table; §12.4 defines overflow as "no eligible worker at the
first scheduling attempt"; D-007 wrote no row for a pass that left a job PENDING.
Decision: a decision row is written for every placement and every reject, plus one at a
job's first attempt when it stays PENDING (it shows why it waited). That first-attempt
row with no worker sets `jobs.overflow=1`. Later no-op passes write nothing (no row per
second per waiting job). "First attempt" = the job has no decision row yet.
Alternatives rejected: a row on every pass (grows ~1/s per waiting job); a separate
`first_attempt` column (the decisions table already records it). Plan §: 6.4, 9, 12.4.

### D-016 (2026-11-15, M3) Scheduler settings: .env defaults, SQLite for the active ones
Context: §7.3 sets the policy with `PUT /scheduler` (409 while jobs are non-terminal);
§7.2 needs `HOST_BUSY_ENABLED/HIGH/LOW`; §6.4 needs `AGING_S`. Where they live is open.
Decision: `.env` keys `DWR_POLICY` (LocalFirst), `DWR_HOST_BUSY_ENABLED` (1),
`DWR_HOST_BUSY_HIGH` (70), `DWR_HOST_BUSY_LOW` (50), `DWR_AGING_S` (30) are defaults.
`PUT /scheduler` stores the active set in a one-row `scheduler_config` table (invariant 5:
a controller restart mid-experiment keeps the policy) and logs a `scheduler_changed`
event. Host-busy state is always tracked and logged (`host_busy`/`host_free` events with
`enforced`); `host_busy_enabled` only decides whether eligibility uses it, so the §12.6
ablation still shows when stress was detected. The 10-sample window is in memory.
Alternatives rejected: settings in memory only (lost on restart). Plan §: 6.4, 7.2, 7.3, 9.

### D-017 (2026-11-15, M3) What aging does
Context: §6.4: "any job pending longer than AGING_S is tried first, so large jobs are not
starved". Aged jobs are the oldest, so in FIFO order they are already tried first;
literal reordering changes nothing. Starvation happens when later small jobs keep taking
the capacity a big job is waiting for (FIFO *with skip*).
Decision: in a pass, once an aged job stays PENDING (not structural), the workers that
could ever run it are held for the rest of that pass: every job behind it, aged or not,
sees reason `reserved_for_aged` there. Aged jobs come first in FIFO order, so the oldest
blocked job wins. (Exempting younger aged jobs was tried and failed: under sustained load
they age too and take the capacity again.) The hold is recomputed every pass.
Alternatives rejected: literal reordering only (no effect); blocking the whole queue
behind an aged job (idles workers it can't use). Plan §: 6.4.

### D-018 (2026-11-15, M3) stress.py on a small Fog host
Context: §12.6 runs stress "pinned to non-slot cores, about 80% host CPU". fog-1 (pop-os)
has 2 cores with slot core 1, so one free core gives at most 50% host CPU, below
`HOST_BUSY_HIGH` = 70.
Decision: `experiments/stress.py` defaults to the non-slot cores and a target host CPU%,
computes the duty per core, and warns when the target is unreachable; `--cpus all`
spreads the load over every core (it then also competes with the slot, which is still
foreign load). The choice for the real experiment is the owner's (see m3 evidence).
Alternatives rejected: silently using all cores. Plan §: 7.2, 12.6.
