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

### D-019 (2026-11-15, M4) Declared base memory 40 → 48 MB
Context: the RSS watchdog kills at 110% of the declared memory (§5.4). Measured peaks of
a cpu_hash job: Windows tree 34.4 MB (interpreter 30.2 + venv launcher 4.2), Linux
interpreter 38.5 MB. With a 40 MB base the Linux limit (44 MB) left 5.5 MB of margin.
Decision: `BASE_MEM_MB = 48` (limit 52.8 MB for cpu_hash). The watchdog sums RSS over
the whole job tree, launcher included. A false `memory_limit` kill would corrupt results;
8 MB more per job in the reservations is cheap.
Alternatives rejected: a fixed slack on top of 110% (departs from the plan's rule).
Plan §: 4, 5.4.

### D-020 (2026-11-15, M4) Worker termination and recovery details
Context: §5.4/§5.5 give the procedure; some cases are open.
Decision:
- Termination is a non-blocking state machine in the supervisor tick (sentinel → 2 s →
  kill tree → verify ≤ 5 s), so one slow kill never blocks other slots or the heartbeat.
- A process that exits 0 with a valid result during its termination is reported
  SUCCEEDED (it really finished); otherwise KILLED(reason) with `termination_verified`
  and `kill_latency_ms` (from the start of termination). Exit 3 is not required:
  a supervisor-initiated termination is KILLED whatever the code.
- Unverified → KILLED with `termination_verified=false` and the slot held as `stuck`; it
  is released when those exact processes (pid + create_time) are gone.
- Timers start at spawn; `timeout_s` is checked before `max_runtime_s`.
- Recovery scope: only processes whose `--job-dir` is in this agent's jobs dir (so the
  -Sim agent and the Edge agent on one host never kill each other's jobs). ACCEPTED rows
  (never spawned) also end FAILED(agent_restart): jobs don't survive a restart.
- A `spawns` table logs every spawn in the step-2 transaction (AC-4.5 evidence).
Plan §: 5.3, 5.4, 5.5.

### D-021 (2026-11-15, M4) Controller reliability details
Context: §6.2/§6.5/§6.6 leave a few choices open.
Decision:
- Cancel: PENDING → CANCELLED at once; other non-terminal jobs get `cancel_requested`
  (repeated in every reply); terminal → 409.
- Staleness: after `STALE_S` without a heartbeat (counted from controller start for a
  worker not yet heard from) → `worker_stale` event, host-busy window cleared, committed
  ASSIGNED/ACCEPTED/RUNNING jobs → UNKNOWN. The next heartbeat logs `worker_fresh`.
- Reconciliation runs on every applied snapshot: an UNKNOWN job not listed → ASSIGNED if
  never acked (redelivered to the same worker), FAILED(lost) if acked.
- `queue_timeout_s` belongs to the scheduler config (per experiment, PUT /scheduler,
  default `DWR_QUEUE_TIMEOUT_S` = 300). `DWR_LOST_TIMEOUT_S` = 300 is a .env setting.
- The UNKNOWN start time is read from job_events (no extra column).
Plan §: 6.2, 6.5, 6.6, 8.2.

### D-022 (2026-11-15, M5) Dashboard data endpoints and Overview definitions
Context: §10 lists the views; §8.2 has no endpoint for a decisions list and lists are
ascending only. Overview terms ("local offload", peak backlog) need exact definitions.
Decision: add `GET /decisions?run_id=&limit=` (newest first, joined with the job's type,
state, mem_mb), `order=desc` on `GET /jobs` and `GET /events`, and `GET /overview?run_id=`
(computed in `controller/overview.py`). Definitions: completed = SUCCEEDED; per tier =
the tier of the job's worker; local offload % = completed jobs (and their `cpu_s`) not
on the lowest-ranked *registered* tier; backlog = PENDING now; peak backlog and
overflow time (queue non-empty) from replaying PENDING entries/exits in job_events;
overflow demand = Σ `exec_s` of overflow jobs. The dashboard is served by the controller
via StaticFiles mounted after the API. A stale worker's card keeps its last telemetry,
labelled as last reported.
Alternatives rejected: computing metrics in the browser (would duplicate §12.4 logic).
Plan §: 8.2, 10, 12.4.

### D-023 (2026-11-15, M5) Legacy move and frontend clean-up
Context: M5 moves the mid-term code to legacy/ and rewires the existing React app.
Decision: `git mv` of the tracked files app/, router/, run_demo.py,
scripts/load_generator.py, scripts/__init__.py, Dockerfile, docker-compose.yml into
legacy/ (paths inside stay relative, so it runs from legacy/). The untracked
`app/scripts/load_test.py` is the owner's and was left where it is. pytest now collects
`legacy/app/Tests`. The React app keeps React + Vite; the scripted demo, the topology
graph, App.css, unused assets, the Google Fonts import and the lucide-react dependency
were removed. The root README gets a pointer that it describes the mid-term prototype
(a V1 README is the owner's call).
Plan §: 10, 11, M5.

### D-024 (2026-11-21, M6) Runs API rules and remote stress timing
Context: §8.2 lists `POST /runs`, `POST /runs/{id}/finish`, `GET /runs/{id}/export`; the
ablation (§12.6) needs stress on the Fog host at a fixed offset into each run, but only the
controller listens and the two clocks are not synchronised.
Decision: `POST /runs` is refused (409) while any job is non-terminal or another run is
open; the stored policy and scheduler settings are the controller's active ones. A job
with a `run_id` must name an open run (422 otherwise). Export: jobs and decisions by
run_id; telemetry samples and events by the run's [start, end] window. `GET /runs` adds
`elapsed_s` (controller clock) for open runs; `stress.py --follow-runs` on the Fog host
polls it (admin token) and starts the stress at `at_s - elapsed_s`, so the ablation runs
unattended. `run_started`/`run_finished` events are logged.
Alternatives rejected: SSH from the harness to the Fog host (a new connection path);
starting stress by hand per run (not repeatable). Plan §: 8.2, 12.3, 12.6.

### D-025 (2026-11-21, M6) Harness and analysis definitions
Context: §12.2–12.5 define the method; several details are open.
Decision:
- Job sizes: three fixed parameter sets per type (gen_trace.VARIANTS), drawn uniformly;
  the reference run measures each on Edge with all slots busy (EdgeOnly, all at t=0).
  Load L ⇒ Poisson rate L × Edge slots / E[exec_s under the mix]. Bursts: 20 s on / 20 s
  off, same mean rate. data_digest transfer time is not part of Edge capacity (localhost).
- One trace per (load, rep), seed = base + 100·rep + load index, replayed by every strategy;
  strategy order rotates per rep. Sweep = EdgeOnly, LocalFirst, RoundRobin (§12.1) ×
  {0.5, 1, 1.5, 2, 3} × 3 reps, 180 s arrivals + 120 s drain; ablation = LocalFirst
  host-busy off/on at load 1.5, 240 s, stress +60 s for 120 s, 3 reps. FogOnly is left out
  of the sweep (§12.1 lists it only as a sanity baseline).
- Leftover jobs after the drain are cancelled and counted as unfinished (not completed,
  not failures). Failure rate = (FAILED + KILLED) / jobs.
- Overflow demand = Σ exec_s of overflow jobs; for overflow jobs that never ran (EdgeOnly
  queue timeouts, drain cancels) the reference Edge time of their size is used and counted
  (`overflow_demand_estimated`); replay copies the reference timings into each manifest so
  analysis needs nothing outside the run directory.
- Percentiles: linear interpolation. Summary = mean and sd over reps per (strategy, load).
  L* = highest load where the strategy is acceptable at it and every lower load.
- The idle rule (§12.3: CPU < 10% for 30 s) is the default; a block config may relax it
  (`idle_cpu_pct`, `idle_quiet_s`) and every run records the rule it used.
- Plots need matplotlib, kept in `requirements-analysis.txt` so workers don't install it.
Plan §: 12.2, 12.3, 12.4, 12.5, 12.7.

### D-026 (2026-11-21, M6) Dispatch overhead and the capacity basis for load levels
Context: the first local pilot showed each job holding its Edge slot ~2.3 s longer than its
`exec_s` (reference run: e.g. 0.05 s data_digest held a slot 2.2–3.3 s). Causes: (a) a freed
slot was only refilled on the *next* heartbeat (~1 s); (b) each job process imported pydantic
(0.47 s of ~1.0 s start-up) just to re-check its parameters. With capacity defined from
`exec_s` (§12.2 literally), "load 1.0" was in fact a heavy overload: nearly every job
overflowed even under LocalFirst.
Decision:
- When an applied heartbeat reports a terminal job (or the worker comes back), the controller
  runs the scheduling pass under the same lock *before* building that reply, so the reply
  carries the next assignment (single writer kept; §6.4 already runs a pass on terminal).
- The job process validates with `shared.catalog.check_params` (same rules, no pydantic); a
  test checks it agrees with `validate_job` on 4 500 random inputs. Controller and agent
  still use `validate_job` (invariant 3 unchanged). Start-up fell from ~1.0 s to ~0.4 s.
  Result: per-job slot overhead ~2.1 s → ~0.8 s.
- The reference run records both `mean_exec_s` and `mean_service_s` (assign → end on the
  controller clock = how long a job really holds an Edge slot). Load levels default to the
  service time (`"capacity_basis": "service"`), so load 1.0 means Edge is saturated; a block
  can set `"capacity_basis": "exec_s"` for the plan's literal definition. Every trace records
  its basis. Overflow demand still uses exec_s (Cloud would need execution time, not our
  dispatch overhead).
- Every run records the controller laptop's power state (`harness_power`): on battery the
  same cpu_hash took ~1.7× longer. The reference and the sweep must run in the same state.
Alternatives rejected: shorter heartbeat interval (more load, still a poll); keeping exec_s
as the basis (makes the low end of the load sweep meaningless). Owner to confirm the basis.
Plan §: 4, 6.4, 12.2. Amends D-025 (capacity definition).

### D-027 (2026-11-21, playground) Classroom playground semantics
Context: the owner asked for a classroom view where a teacher starts activities or single
tasks and watches real jobs on both laptops, with a "Cloud" part shown as a queue the
laptops pick from. Not part of PROJECT_PLAN_V1 (supports the M7 demo); plan not edited.
Decision:
- The "Cloud overflow queue" is the controller's real PENDING queue, labelled as the work a
  Cloud tier would receive in V2; no simulated cloud worker. Laptops pick from it through the
  normal scheduler (inline on the freeing heartbeat, D-026).
- A job running on a laptop that drops off stays UNKNOWN on that laptop ("offline: still
  running there, result pending") and is never re-queued: at-most-once (invariant 7),
  confirmed by the owner. Jobs assigned but never delivered are redelivered (D-021).
- Activities and hands-on tasks are tagged runs (block "playground"); jobs go through
  `service.submit`. One activity at a time (409 otherwise). The first hands-on click when idle
  opens a "free play" run. A restart closes an open playground run and cancels its
  unfinished jobs, so a class never inherits old work.
- Presets: Quiet day, Edge saturates, Rush hour, Without Fog (same seed as Edge saturates, for
  a fair comparison), Fog gets busy, Both laptops busy, Fog disconnects (timed cue cards; the
  Wi-Fi is switched by hand), Cancel a long job (30 s: mem_touch's hold limit). Load levels use
  experiments/reference.json, else built-in mains timings.
- Fog stress comes from `stress.py --follow-runs --block playground` on the Fog laptop; Edge
  stress for "Both laptops busy" is a subprocess the runner starts and kills. Under it the
  controller laptop itself is at ~85% CPU (it is the Edge laptop): the UI gets slower.
- All teacher-facing wording (story feed, "Why here?" cards, labels, reason texts) is built
  by controller/playground.py, so `frontend/src` still holds no tier names (AC-5.1).
Alternatives rejected: re-queueing an interrupted job (could run twice); a fake cloud
worker (would invent results); browser-side activity timing (stops when the tab closes).

### D-028 (2026-11-21, playground) Live per-job stats and the job's slot
Context: the playground shows each running job's slot, cores, CPU% and RAM.
Decision: `JobSnapshot` gains optional `cpu_pct` (tree CPU, 100 = one core) and `rss_mb`,
sampled by the supervisor's 250 ms watchdog pass and sent for RUNNING jobs. They are optional
and unknown fields are ignored, so old and new agents/controllers interoperate and
SCHEMA_VERSION stays 1. The controller keeps them in memory only (`WorkerRuntime.job_stats`):
live values, not records. The slot the agent already reported is now stored in a new
nullable `jobs.slot` column; `Database()` adds missing columns to an existing file
(`MIGRATIONS`). Agent version 1.0.0-m7. Job processes validate with check_params (D-026).
Alternatives rejected: persisting live stats (noise in the source of truth); computing CPU
on the controller (it never talks to workers).

### D-029 (2026-11-21, playground) Own CPU from CPU-time deltas
Context: the agent's `own_cpu_pct` used psutil's per-object `cpu_percent()`, which returns 0
the first time it sees a process. With ~1 s heartbeats and 1–3 s jobs, most job CPU was
missed and reported as *foreign* (Workers view: "CPU (DWR jobs) 0%" with two jobs running), so
host-busy (§7.2) could count our own jobs as other programs.
Decision: `TelemetrySampler` sums each job tree's CPU-time growth since the previous sample
(a process created since then counts in full) and divides by the wall interval × cores.
Measured on fog-1 under load: host 52%, own 50%, foreign 2%. CPU of a job that exits
between two samples is still missed (it shows as foreign for one sample).
Alternatives rejected: sampling more often (more load, same first-sample blind spot).
Plan §: 5.2, 7.2.

### D-030 (2026-11-22, playground) Live map animated from consecutive polls
Context: the owner asked for a more visual, easier Playground; chose a live network map plus
polish. The API is polled every 1 s; there is no push channel.
Decision: `frontend/src/views/liveDiff.js` diffs two consecutive `/playground/live` payloads
into moves (new job → controller to laptop or queue; queue → laptop = picked; newly finished
→ coloured at its laptop); the map animates only those. A job that starts and ends between
two polls appears via `finished` (flies already coloured). Narration lines gain `kind` and
`worker_id`; hints and legend wording come from the API (AC-5.1). Each laptop's accent colour
is its position in the API's worker order (tier_rank), never its tier name. The queue
sparkline plots polled values only, per activity.
Alternatives rejected: a server event stream (new transport for a view-only feature); a chart
or graph library (offline demo, no new dependencies).
Plan §: 10 (dashboard), M7 demo.

### D-031 (2026-11-22, playground) Policy picker for hands-on clicks
Context: the owner tried RoundRobin from the Custom panel while a free-play session (opened by
a task click) was active; the page disabled every start button, so it looked as if policies
did nothing. Hands-on clicks also had no way to choose a policy.
Decision: free play never disables the activity buttons (the server already closes an idle
free-play run, and answers 409 with a reason otherwise). `POST /playground/policy` sets the
policy for clicks: an idle free-play run is closed so every run keeps one policy, and the next
click opens a new one; 409 while a preset/custom activity or free-play tasks are running.
Checked on the real laptops: RoundRobin placed clicks edge-1, fog-1, edge-1 (fog-1 full: edge-1).
Alternatives rejected: changing the policy of a running run (its comparison row would mix policies).
Plan §: 6.4 (policies), M7 demo.
