# Dynamic Workload Router: V1 Project Plan (Edge + Fog)

**Status:** Draft for owner approval. Nothing in this document has been implemented.

**Baseline:** repository at commit `b5da067`.

**Related documents:**
- `PLAN_CRITIQUE.md` and `V1_CRITIQUE.md`: the reviews this plan responds to.
- `PROJECT_PLAN_v2.md`: kept as the **reference design for later versions** (EFT, calibration, Cloud). It is not the V1 scope.

**Owner decisions included:**
- Edge and Fog only; no Cloud VM in V1.
- The scheduling algorithm will iterate in later versions.
- HTTP pull heartbeat. Redis Streams is planned for V2.
- The host-busy (high foreign CPU) eligibility rule is included.

---

## 1. Research motivation and V1 question

Cloud applications absorb load spikes by scaling out replicas (KEDA/HPA), which consumes extra Cloud compute. V1 investigates the step **before** scale-out: using spare capacity on ordinary local devices.

**V1 research question:**
> How much of the overflow demand that would otherwise require Cloud scale-out can be absorbed by adding an ordinary second local device, using a simple local-first policy, and at what cost in completion time and reliability?

Without a Cloud VM, V1 measures **counterfactual Cloud demand**: the work that found no eligible local worker. That is exactly the backlog an autoscaler such as KEDA would have reacted to (§12).

**Positioning, stated honestly:**
- Overflow/spill-over offloading is a known idea (see the references in §15).
- Our contribution is a working lightweight implementation on real heterogeneous machines and a measured trade-off curve.

---

## 2. Scope

### In V1

| Area | Contents |
|---|---|
| Machines | Windows laptop: controller plus Edge worker agent. Linux laptop: Fog worker agent |
| Workloads | `cpu_hash`, `data_digest`, `mem_touch`, each a real subprocess, from a fixed catalog |
| Communication | HTTP pull heartbeats started by the workers; only the controller listens |
| Scheduling | A replaceable scheduler interface; `LocalFirst` (Edge → Fog → queue) plus baselines `EdgeOnly`, `FogOnly`, `RoundRobin` |
| Eligibility | Freshness, draining, type support, free slot, memory budget, free-memory floor, **host-busy rule** |
| Reliability | Stale detection, UNKNOWN and reconciliation, timeouts, cancellation, verified termination, duplicate protection, agent and controller restart safety, RSS watchdog |
| Persistence | Controller SQLite; worker SQLite journal |
| Dashboard | Workers, Jobs, Decisions, Overview, all from real data |
| Evaluation | Seeded traces, open-loop replay, CSV output, load sweep, overflow metrics, host-busy experiment |
| Install | `setup.ps1` / `start.ps1` / `stop.ps1` (with a `-Sim` mode), `setup.sh` plus a systemd unit |

### Not in V1 (see §16 for later versions)

- Cloud worker, Redis Streams, Kubernetes and KEDA.
- EFT, calibration, EWMA prediction, bandwidth prediction, deadline optimisation and cost weighting.
- CPU quotas, cgroups, Job Objects and RLIMIT_AS.
- Automatic re-placement of jobs onto another worker.
- ML anomaly detection and Q-learning.
- CLI, Windows service, WebSockets/SSE, Docker on the critical path.

---

## 3. Architecture

```
 Windows laptop                                              Linux laptop
┌──────────────────────────────────────────┐               ┌──────────────────────────────┐
│ Controller process (FastAPI :8000)       │◄─ heartbeat ──│ Worker agent  tier=fog       │
│  • /api/v1  (UI, workers, jobs, runs)    │   every 1 s   │  • heartbeat loop            │
│  • scheduler loop  (single writer)       │   (LAN or     │  • supervisor (slots, kill)  │
│  • SQLite WAL  (jobs, events, decisions) │   Tailscale)  │  • journal (SQLite)          │
│  • payload blob store                    │── reply: ────►│  • job subprocesses          │
│  • serves built dashboard (dist/)        │  assignments, └──────────────────────────────┘
│  pinned away from the Edge slot cores    │  cancels, acks
└──────────────▲───────────────────────────┘
               │ heartbeat via 127.0.0.1
┌──────────────┴───────────────────────────┐
│ Worker agent  tier=edge  (own process)   │  slots pinned to dedicated cores
└──────────────────────────────────────────┘
```

**Invariants**
1. **Only the controller listens** (TCP 8000, bound to `127.0.0.1` plus the LAN or Tailscale IP). The controller never initiates a connection to a worker.
2. **The controller clock is the authority.** Submit, assign, start and end times are recorded **when the controller observes them**. Workers report only durations measured on their own monotonic clock (`exec_s`, `transfer_s`, `spawn_s`).
3. **Jobs are instructions, not code.** A job is a catalog type plus bounded parameters, validated on the controller and again on the worker.
4. **Each job is its own subprocess.** Nothing CPU-heavy runs in an event loop.
5. **SQLite is the source of truth on both sides.** In-memory state is a cache that can always be rebuilt.
6. **Payload data originates on the controller host.** Edge reads it locally; Fog downloads it.
7. **Delivery guarantee:** **at-most-once execution with no automatic retry.** Exactly-once is never claimed.

**Tech stack:** Python 3.10–3.12, FastAPI, Uvicorn, Pydantic v2, httpx, psutil, `sqlite3` (standard library), React/Vite (the existing frontend). All versions are pinned in `requirements.txt`.

---

## 4. Workload catalog

Each workload is launched as `python -m worker.workloads <type> --job-id <id> --job-dir <dir> --cpus <list> --params <json>`.

| Type | Parameters (bounds) | Declared memory | What `exec_s` covers |
|---|---|---|---|
| `cpu_hash` | `iterations` 10⁴ – 5·10⁶ | 40 MB | Hash loop |
| `data_digest` | `input_bytes` 0 – 50 MB, `rounds` 1 – 20 | 40 MB + `input_bytes` | Reading `input.bin` and hashing it `rounds` times (download is measured separately as `transfer_s`) |
| `mem_touch` | `mem_mb` 16 – 1024, `hold_s` 0 – 30 | `mem_mb` + 40 MB | Allocation and page-touch (`touch_s`) **plus** hold (`hold_s_actual`); `exec_s` = `touch_s` + `hold_s_actual` |

**Common job fields**
- `max_runtime_s`: per-type catalog cap. `cpu_hash` and `data_digest` default to 120; `mem_touch` defaults to `hold_s` + 60.
- `timeout_s`: optional. Must be ≤ `max_runtime_s`.
- `placement`: `"auto"`, or a specific `worker_id`. A specific worker still goes through the eligibility filter.

**Workload process contract**
1. **Pin first.** The process pins itself to `--cpus` with `psutil.Process().cpu_affinity()` **before** any timed work.
2. **Cancel sentinel.** It checks for `<job-dir>/cancel` at least every 100 ms, including **during the `mem_touch` hold**, by sleeping in steps of 100 ms or less.
3. **Atomic result.** It writes `result.json` atomically (`.tmp` then `os.replace`). Fields:
   - `exec_s`
   - `cpu_s` (from `time.process_time()`)
   - `peak_rss_mb` (Linux: `ru_maxrss`; Windows: `peak_wset`, labelled "working set")
   - `checksum`
   - type-specific fields (`touch_s`, `hold_s_actual`, `bytes_hashed`)
4. **Exit codes.** 0 = success; 2 = invalid parameters; 3 = cancelled cooperatively; 1 = other error.
5. **Single-threaded and pure Python.** No numpy and no thread pools, so one slot equals one core.

**Test-only:** `mem_touch.actual_mb_override` lets the watchdog demo allocate more memory than declared. The controller only accepts it when started with `DWR_TEST_MODE=1`.

---

## 5. Worker agent

### 5.1 Configuration (`.env`)

```
DWR_WORKER_ID=fog-1
DWR_TIER=fog                       # edge | fog   (cloud reserved for V2)
DWR_CONTROLLER_URL=http://<controller-ip>:8000
DWR_WORKER_TOKEN=<secret>
DWR_SLOT_CPUS=1;2                  # one entry per slot; ";" separates slots, "," separates cores within a slot
DWR_MEM_BUDGET_MB=1536
DWR_MEM_FLOOR_MB=512               # minimum host free memory that must remain after admission
DWR_DATA_DIR=./var/worker
```

### 5.2 Heartbeat loop

- At startup the agent generates a new `agent_instance_id` (UUID). Every heartbeat gets a strictly increasing `seq`.
- It sends a heartbeat every **1 s**, plus an **immediate** heartbeat whenever a job reaches a terminal state.
- **At most one heartbeat is in flight.** The agent fully processes each reply (writing assignments to the journal) **before** it sends the next heartbeat. A reply to a request that timed out is discarded.
- **Telemetry in each heartbeat:**
  - `cpu_pct` (whole host, 0–100)
  - `own_cpu_pct` (sum over DWR job processes, normalised to the whole host)
  - `foreign_cpu_pct` = max(0, `cpu_pct` − `own_cpu_pct`)
  - `mem_total_mb`, `mem_available_mb`
  - `free_slots`
  - `rtt_ms_last`
- **Job snapshot:** every non-terminal job, plus terminal jobs the controller has not yet acknowledged.

### 5.3 Journal (`<data_dir>/journal.db`, WAL mode, `synchronous=FULL`)

```
jobs(job_id PK, spec_json, state, slot, pid, pid_create_time, job_dir,
     transfer_bytes, transfer_s, spawn_s, exit_code, result_json, error,
     fail_reason, kill_reason, termination_verified, kill_latency_ms,
     reported_terminal INTEGER DEFAULT 0)
```

**Write-ahead order.** Each step is committed before the next action.

| Step | Action |
|---|---|
| 1 | Assignment received → `INSERT state=ACCEPTED`. A `job_id` that already exists is ignored (**duplicate protection**). If the same reply also cancels the job → `state=CANCELLED`, never spawned |
| 2 | A slot frees up → download input (if any) → spawn → **immediately** `UPDATE pid, pid_create_time, state=RUNNING` |
| 3 | Process exits → read `result.json` → `UPDATE` with the terminal state and its fields |
| 4 | Controller sends `ack_terminal` → `reported_terminal=1`. Rows that have been acknowledged are pruned after 24 h |

### 5.4 Supervisor

**Slot discipline.** The supervisor never runs more than its configured number of slots. Each slot is tied to its own set of cores.

**Timers**
- `max_runtime_s` and `timeout_s` are measured from spawn, on the worker's monotonic clock.

**RSS watchdog**
- Polls every 250 ms.
- If RSS exceeds 110% of the declared memory, the job is killed as `memory_limit`.
- This is **not a hard limit**: memory can overshoot between polls.

**Termination procedure** (used for cancel, timeout, max runtime and memory):
1. Create the `cancel` sentinel file.
2. Wait 2 s.
3. Kill the process tree (`psutil` `kill()`; Windows has no SIGTERM).
4. Confirm the process is gone (same `pid` **and** `create_time`) by polling for up to 5 s.
5. Record `termination_verified` and `kill_latency_ms`.

If the process is still alive after step 4, the slot is marked `stuck`, an event is reported, and the slot is not reused.

**Exit classification**

| Situation | State |
|---|---|
| Exit 0 and a valid result | `SUCCEEDED` |
| Exit code 3 after a cancel request | `KILLED(cancel)` |
| Supervisor-initiated kill | `KILLED(<reason>)` |
| Any other exit | `FAILED(exit_nonzero)` |
| Spawn error | `FAILED(spawn_error)` |
| Download error | `FAILED(input_transfer_error)` |

### 5.5 Agent restart recovery

Jobs do **not** survive an agent restart; re-adopting orphaned processes is deferred. At startup, before the first heartbeat, the agent does the following for every non-terminal journal row:

1. **Kill surviving processes.** Find one matching `pid` + `pid_create_time` + `--job-id <id>` in its command line. If found, kill it and verify it is gone.
2. **Kill unjournaled processes.** Scan for any process with a `--job-id` marker that the journal doesn't know about (a crash between spawn and step 2) and kill it, verified.
3. **Decide the outcome.** If a complete `result.json` exists, report the real outcome. Otherwise report `FAILED(agent_restart)`.

On Linux, systemd's default `KillMode=control-group` already kills job processes when the unit stops. On Windows, steps 1–2 are what prevent orphans.

---

## 6. Controller

### 6.1 Heartbeat handling

| Case | Action |
|---|---|
| Same `instance`, `seq > last_seq` | Apply the snapshot, then build the reply |
| Same `instance`, `seq ≤ last_seq` (duplicate or reordered) | Do **not** apply the snapshot; return the current reply. This is safe because replies are level-triggered |
| New `instance` | Record an `agent_restarted` event and apply the snapshot |
| Unknown worker or wrong token | 401 |

**Replies are level-triggered.** Every reply contains:
- **all** jobs assigned to that worker that its snapshot does not yet list,
- **all** of its non-terminal jobs that have `cancel_requested`,
- `ack_terminal` for terminal results the controller has stored.

As a result, a lost reply is simply repeated next time, and a duplicate assignment is deduplicated by the worker's journal.

### 6.2 Job state machine

**States**
- Non-terminal: `PENDING`, `ASSIGNED`, `ACCEPTED`, `RUNNING`, `UNKNOWN`
- Terminal (immutable): `SUCCEEDED`, `FAILED`, `KILLED`, `CANCELLED`, `REJECTED`

**Flags:** `cancel_requested`; `acked` (true once the worker has listed the job in a snapshot).

**Reasons**
- `fail_reason` ∈ {`exit_nonzero`, `spawn_error`, `input_transfer_error`, `worker_validation`, `agent_restart`, `queue_timeout`, `lost`}
- `kill_reason` ∈ {`cancel`, `timeout`, `max_runtime`, `memory_limit`}

| From | To | Trigger |
|---|---|---|
| — | PENDING | Valid submission (an invalid one gets HTTP 422 and no record) |
| PENDING | ASSIGNED | Scheduler placement (in one transaction with the decision record) |
| PENDING | REJECTED | Structurally impossible: no registered worker supports the type, or the memory exceeds every worker's budget |
| PENDING | CANCELLED | Cancel requested |
| PENDING | FAILED(`queue_timeout`) | Pending longer than `queue_timeout_s` |
| ASSIGNED | ACCEPTED / RUNNING / SUCCEEDED / FAILED / KILLED / CANCELLED | Worker snapshot (fast jobs may skip states) |
| ASSIGNED | UNKNOWN | Worker became stale |
| ACCEPTED | RUNNING / SUCCEEDED / FAILED / KILLED / CANCELLED | Worker snapshot |
| ACCEPTED | UNKNOWN | Worker became stale |
| RUNNING | SUCCEEDED / FAILED / KILLED | Worker snapshot |
| RUNNING | UNKNOWN | Worker became stale |
| UNKNOWN | ACCEPTED / RUNNING / SUCCEEDED / FAILED / KILLED / CANCELLED | Snapshot after the worker reconnects |
| UNKNOWN | ASSIGNED | Worker is fresh again, the job is not in its snapshot, and the job was **never acked**. Delivery resumes to the **same** worker |
| UNKNOWN | FAILED(`lost`) | The job was acked but is missing from the reconnected snapshot (journal lost), **or** the worker stayed stale longer than `LOST_TIMEOUT_S` |
| any terminal | — | Forbidden. Data that arrives late is stored in `late_result_json` plus an event |

**Implementation and tests.** The table lives in `shared/states.py`. Every change goes through `transition()`, which writes a `job_events` row. Unit tests cover all 10 × 10 state pairs.

### 6.3 Reservations and the single writer

- **Reservations are derived, not counted.** A worker's committed jobs are its jobs in {ASSIGNED, ACCEPTED, RUNNING, UNKNOWN}. Used slots and used memory are calculated from that set, so there is no counter to drift. Reaching a terminal state releases the reservation automatically. UNKNOWN jobs keep their reservation (the conservative choice).
- **One writer.** Every database write (scheduler pass, heartbeat application, cancel, staleness sweep, queue timeout) runs under **one asyncio lock**.
- **One transaction per placement:** `BEGIN IMMEDIATE` → read committed jobs → `decide()` → insert the decision → transition to ASSIGNED → `COMMIT`.
- **No worker-side queue in V1.** A job is assigned only when a slot is free; all overflow waits in the controller's PENDING queue. This makes "overflow" precisely measurable (§12).

### 6.4 Scheduler loop

- **When it runs:** on job submission, on any terminal transition, on a worker becoming fresh or eligible again, and every 1 s as a fallback.
- **Queue order:** PENDING jobs are scanned in **FIFO with skip**. Every pass looks at the whole queue, so a job that can't be placed doesn't block the ones behind it.
- **Aging:** any job pending longer than `AGING_S` (default 30 s) is tried first, so large jobs are not starved.

### 6.5 Background tasks

| Task | Rule |
|---|---|
| Staleness sweep (every 1 s) | Heartbeat age > `STALE_S` (5 s) → worker becomes stale and its committed jobs become UNKNOWN |
| Lost timeout | UNKNOWN for longer than `LOST_TIMEOUT_S` (300 s) → `FAILED(lost)`. Assignments are no longer delivered to that worker |
| Queue timeout | PENDING for longer than `queue_timeout_s` (default 300 s; configured per experiment) → `FAILED(queue_timeout)` |
| Telemetry sampling | One row per worker every 5 s into `telemetry_samples` |

### 6.6 Controller restart

- Startup rebuilds the cache from SQLite.
- Workers are treated as "freshness unknown" until their first heartbeat. Any worker not heard from within `STALE_S` is marked stale and its jobs go to UNKNOWN.
- PENDING jobs resume.
- `last_seq` is not persisted, so the first heartbeat from each worker is accepted as new.

---

## 7. Scheduling

### 7.1 Interface (pure, no I/O, in `scheduler/`)

```python
@dataclass(frozen=True)
class WorkerView:   # built by the controller from SQLite + latest heartbeat
    worker_id: str; tier: str; fresh: bool; draining: bool; host_busy: bool
    supported_types: frozenset[str]; slots_total: int; slots_used: int
    mem_budget_mb: int; mem_committed_mb: int; mem_available_mb: int; mem_floor_mb: int

@dataclass(frozen=True)
class JobView:
    job_id: str; type: str; mem_mb: int; placement: str   # "auto" | worker_id

@dataclass(frozen=True)
class Decision:
    worker_id: str | None          # None → stay PENDING
    policy: str
    eligible: tuple[str, ...]
    ineligible: dict[str, str]     # worker_id → reason code
    structural_reject: bool        # True → REJECTED (no worker could ever run it)

class Scheduler(Protocol):
    name: str
    def decide(self, job: JobView, workers: list[WorkerView]) -> Decision: ...
```

**One shared eligibility filter** (`scheduler/eligibility.py`) is used by every policy. It returns a reason code for each ineligible worker:

| Order | Check | Reason code |
|---|---|---|
| 1 | Worker is fresh | `stale` |
| 2 | Worker is not draining | `draining` |
| 3 | Worker supports the job type | `unsupported_type` |
| 4 | `slots_used < slots_total` | `no_free_slot` |
| 5 | `mem_committed_mb + job.mem_mb ≤ mem_budget_mb` | `mem_budget` |
| 6 | `mem_available_mb − job.mem_mb ≥ mem_floor_mb` | `mem_floor` |
| 7 | **Host-busy check (§7.2)** | `host_busy` |
| 8 | Placement constraint | `not_requested` |

### 7.2 Host-busy rule (high foreign CPU)

**Purpose.** Use a local device only when it actually has spare CPU, rather than only when DWR's own slots are free.

**What is measured.** Each heartbeat carries `foreign_cpu_pct` = host CPU% − CPU% of DWR job processes, both normalised to the whole host (§5.2).

**Decision on the controller, per worker:**
- Keep the last 10 samples (about 10 s).
- **Become busy** when at least 5 samples exist **and** their mean is ≥ `HOST_BUSY_HIGH` (default 70).
- **Become free** when the mean is ≤ `HOST_BUSY_LOW` (default 50).
- Between the two thresholds, the previous state is kept. This hysteresis prevents flapping.

**Effects:**
- A busy worker gets no **new** assignments. Running jobs are not touched.
- Transitions are logged as events and shown on the dashboard.
- The rule can be turned off per experiment with `HOST_BUSY_ENABLED`, which allows the ablation in §12.

**Limitations (documented, not hidden):**
- Detection lags by roughly 5–10 s.
- On the Windows Edge, the controller, browser and harness count as "foreign" load. That is accurate, but keep them light during experiments.
- The CPU% readings are sampled, so very short spikes are invisible.

### 7.3 Policies in V1

| Policy | Rule among eligible workers | Use |
|---|---|---|
| `LocalFirst` | Lowest tier rank (edge = 0, fog = 1; cloud = 2 is reserved), then `worker_id` | **Proposed system** |
| `EdgeOnly` | The Edge worker only, otherwise stay PENDING | Baseline: a single device with no offloading |
| `FogOnly` | The Fog worker only | Sanity baseline |
| `RoundRobin` | Cycle through the eligible workers | Shows whether priority matters |

**Decision when nothing fits:**
- No eligible worker → `worker_id=None`, and the job stays PENDING.
- If the cause is structural (§6.2) → `structural_reject=True`, and the job becomes REJECTED.

**Choosing a policy.** The active policy is set with `PUT /api/v1/scheduler`. That call is refused with 409 while any non-terminal job exists, so each run uses exactly one policy.

---

## 8. API contracts (`/api/v1`)

**Common rules**
- **Auth:**
  - Worker endpoints need `Authorization: Bearer <token>`, with the token bound to a `worker_id` in the controller's `.env`.
  - UI and admin endpoints are accepted only from `127.0.0.1`, or with an `X-Admin-Token` header.
- **Versioning:** every request and reply carries `schema_version`. The controller rejects an incompatible agent with 409.
- **Errors:** 401 auth, 404 unknown, 409 conflict or illegal transition, 422 validation, 503 database unavailable.

### 8.1 Worker-facing

| Method and path | Request → Response |
|---|---|
| `POST /workers/register` | `{worker_id, tier, hostname, os, os_version, python, cpu_logical, mem_total_mb, slots:[{slot, cpus}], mem_budget_mb, mem_floor_mb, supported_types, capabilities:{affinity:true, cpu_quota:"not_enforced", rss_watchdog:true, memory_hard_limit:"not_enforced"}, agent_version, agent_instance_id}` → `{accepted, heartbeat_interval_s, stale_after_s}` |
| `POST /workers/{id}/heartbeat` | `{agent_instance_id, seq, telemetry:{cpu_pct, own_cpu_pct, foreign_cpu_pct, mem_total_mb, mem_available_mb, free_slots, rtt_ms_last}, jobs:[{job_id, state, slot, pid, pid_create_time, transfer_bytes, transfer_s, spawn_s, exit_code, result, error, fail_reason, kill_reason, termination_verified, kill_latency_ms}]}` → `{seq_ack, server_processing_ms, drain, assignments:[{job_id, type, params, input_bytes, input_sha256, input_url, max_runtime_s, timeout_s, mem_mb}], cancel:[job_id], ack_terminal:[job_id]}` |
| `GET /jobs/{id}/input` | Raw bytes with `Content-Length` and no compression. Only the assigned worker may fetch them |

### 8.2 UI and harness

| Method and path | Purpose |
|---|---|
| `GET /health` | `READY` / `DEGRADED` / `NOT_READY`, with reasons |
| `GET /catalog` | Workload types, parameter bounds and defaults |
| `POST /jobs` | `{type, params, timeout_s?, placement?, run_id?, trace_index?}` → 202 `{job_id, state}` |
| `GET /jobs?state=&worker=&run_id=&limit=&after=` | List jobs |
| `GET /jobs/{id}` | The job, its transitions and its decision |
| `POST /jobs/{id}/cancel` | 202 `{cancel_requested: true}`. This means **requested**, not confirmed |
| `GET /workers`, `GET /workers/{id}` | Registry, freshness, host-busy state, slots, memory, latest telemetry |
| `POST /workers/{id}/drain`, `POST /workers/{id}/enable` | Stop or resume new assignments |
| `GET /scheduler`, `PUT /scheduler` | Active policy and host-busy settings. `PUT` → 409 while any job is non-terminal |
| `GET /overview` | Totals, counts per state, Edge/Fog share by job count and by CPU-seconds, current backlog, overflow counters |
| `GET /events?after=` | Event feed |
| `POST /runs`, `POST /runs/{id}/finish`, `GET /runs/{id}/export?table=jobs\|decisions\|telemetry\|events` | Tag experiment runs and export their CSV |

---

## 9. Persistence (controller SQLite, WAL)

| Table | Contents |
|---|---|
| `workers` | ID, tier, static capabilities, slots and their cores, budgets, token hash, `registered_at` |
| `jobs` | Spec, state, `fail_reason`/`kill_reason`, `worker_id`, `acked`, `cancel_requested`; controller timestamps for submit, assign, ack, start and end; `transfer_bytes`, `transfer_s`, `spawn_s`, `exec_s`, `cpu_s`, `peak_rss_mb`; `pid`, `hostname`; `overflow` flag; `run_id`, `trace_index`; `late_result_json` |
| `job_events` | `job_id`, timestamp, from, to, trigger, reason |
| `decisions` | `job_id`, timestamp, policy, chosen worker, eligible JSON, ineligible JSON (with reasons), `decision_us` |
| `telemetry_samples` | `worker_id`, timestamp, `cpu_pct`, `foreign_cpu_pct`, `mem_available_mb`, `slots_used`, `host_busy` |
| `runs` | `run_id`, name, policy, trace hash, config JSON, git commit, start and end |
| `events` | Timestamp, kind, `worker_id`, `job_id`, details |

---

## 10. Dashboard

Rewire the existing React app to fetch from `/api/v1` on the same origin. It polls every 1 s and is served by the controller from `frontend/dist`.

| View | Contents |
|---|---|
| **Workers** | ID, hostname, OS, tier, online/stale, last-heartbeat age, CPU%, foreign CPU%, **host-busy badge**, RAM%, slots free/total, active jobs, capabilities (with `not_enforced` shown honestly) |
| **Jobs** | ID, type, worker, real hostname and PID, state with reason, submit/start/end times, `exec_s`, `transfer_s`, cancel button |
| **Decisions** | For each job: policy, chosen worker, eligible workers, ineligible workers with reason codes, decision time |
| **Overview** | Totals and per-state counts, completed on Edge vs Fog (by count and by CPU-seconds), local-offload % (share of work done on Fog), current backlog, overflow jobs, peak backlog |
| **Submit** | A form generated from `/catalog`, with bounded inputs and a placement selector |

**Removed:** the scripted demo, every hardcoded tier, score, RPS, p99 and offload value, and the fixed `API_BASE`. A stale worker is visibly shown as stale.

---

## 11. Repository structure

The mid-term code stays untouched until M5, protected by the tag `midterm-baseline`. At M5 it moves to `legacy/` with `git mv`, so its history is kept.

```
dynamic-workload-router/
├── shared/
│   ├── catalog.py          # types, bounds, declared memory, max_runtime defaults
│   ├── schemas.py          # Pydantic: JobSpec per type, Register, Heartbeat, Reply, Snapshot
│   └── states.py           # JobState, transition table, transition(), IllegalTransition
├── scheduler/              # pure; no I/O
│   ├── views.py            # WorkerView, JobView, Decision
│   ├── eligibility.py      # shared filter + reason codes
│   ├── hostbusy.py         # window + hysteresis (pure state machine)
│   └── policies.py         # LocalFirst, EdgeOnly, FogOnly, RoundRobin; registry by name
├── controller/
│   ├── main.py             # app factory, static dist/, restricted CORS
│   ├── settings.py         # .env validation; fail fast with a clear message
│   ├── db.py               # schema, WAL, single-writer lock, transactions
│   ├── api_workers.py      # register, heartbeat, input download
│   ├── api_jobs.py         # submit, list, get, cancel
│   ├── api_admin.py        # workers, drain/enable, scheduler, overview, events, runs/export, health
│   ├── service.py          # scheduler loop, staleness sweep, lost/queue timeouts, telemetry sampling
│   └── blobs.py            # deterministic payload generation (seeded) and storage
├── worker/
│   ├── agent.py            # register, heartbeat loop, instance id/seq, event-triggered heartbeat
│   ├── journal.py          # §5.3
│   ├── supervisor.py       # slots, spawn, timers, watchdog, termination, verification
│   ├── recovery.py         # §5.5
│   ├── telemetry.py        # host + own + foreign CPU, memory
│   └── workloads/          # __main__.py, cpu_hash.py, data_digest.py, mem_touch.py, common.py
├── experiments/
│   ├── gen_trace.py        # seeded trace → CSV + sha256
│   ├── replay.py           # open-loop submission, run tagging, drain, export
│   ├── stress.py           # controlled foreign-CPU load for the host-busy experiment
│   ├── analyze.py          # CSV → tables + plots
│   └── configs/*.json
├── frontend/               # existing app, rewired
├── deploy/systemd/dwr-worker.service
├── scripts/                # setup.ps1, start.ps1 (-Sim), stop.ps1, setup.sh, doctor.py (minimal)
├── tests/unit/  tests/integration/
├── docs/evidence/          # per-milestone evidence
├── legacy/                 # (M5) mid-term app/, router/, run_demo.py, scripts/load_generator.py, compose
├── .env.example  requirements.txt  pyproject.toml  CLAUDE.md  README.md
└── PROJECT_PLAN_V1.md  PROJECT_PLAN_v2.md  PLAN_CRITIQUE.md  V1_CRITIQUE.md
```

---

## 12. Evaluation (V1)

### 12.1 Strategies compared

| Strategy | Role |
|---|---|
| `EdgeOnly` | A single local device with no offloading. Its overflow is the work that would need Cloud scale-out |
| `LocalFirst` | Edge → Fog. **Proposed** |
| `RoundRobin` | Shows whether Edge-first priority matters |
| `LocalFirst`, host-busy **off** vs **on** | Ablation with stress injected on Fog |

### 12.2 Traces and load

- **Job mix:** `cpu`, `mem`, `data`, `mixed` (40/30/30).
- **Arrival pattern:** steady Poisson, or on/off **bursts**.
- **Load levels:** expressed relative to Edge's capacity, for example 0.5×, 1×, 1.5×, 2×, 3×.
  - Edge capacity is measured once in a **reference timing run** (the mean `exec_s` for each type on Edge).
  - This is only used to define load levels. It is not scheduler calibration.
- **Seeds:** traces are generated from a seed, stored as CSV, and hashed. **Every strategy replays the same files.**

### 12.3 Run rules (identical for every strategy)

| Rule | Setting |
|---|---|
| Submission | Open-loop: each job is submitted at its trace time, regardless of completions |
| Configuration | Same slots, pinned cores, budgets, `queue_timeout_s`, `STALE_S` and host-busy settings |
| Run length | Trace duration plus a 120 s drain. Unfinished jobs are counted as not completed |
| Between runs | Wait until no job is non-terminal **and** every worker's CPU has stayed below 10% for 30 s |
| Repetitions | 3 per configuration; the order of strategies rotates each repetition |
| Recorded per run | Git commit, config, trace hash, slot and core configuration, network type (LAN or Tailscale direct/relay), median RTT |

### 12.4 Metrics

**Per job** (`jobs.csv`):
- placement and tier
- `queue_wait_s`, `transfer_s`, `transfer_bytes`, `exec_s`, `cpu_s`
- completion time (controller end − controller submit)
- state and reason
- `overflow` flag

**Per run:**
- **Completion time:** mean and p95.
- **Reliability:** failure rate (FAILED, KILLED and `queue_timeout` all count).
- **Output:** jobs completed within the run length, and makespan.
- **Placement:** Edge/Fog share by job count and by CPU-seconds.
- **Traffic:** network bytes transferred.
- **Hosts:** CPU% and RAM% per machine over time.

**Overflow (counterfactual Cloud demand):**

| Metric | Definition |
|---|---|
| `overflow_jobs` | Jobs that had **no eligible worker** at their first scheduling attempt. These are the jobs a Cloud tier would have received |
| `overflow_demand_s` | Σ `exec_s` of the overflow jobs. Approximates the Cloud slot-seconds needed; Cloud speed differs, so this is stated as an approximation |
| `peak_backlog` | Maximum length of the PENDING queue during the run |
| `overflow_time_s` | Total time the PENDING queue was non-empty. This is how long scale-out would have been active |

### 12.5 Acceptability (SLO; owner to confirm the values)

A strategy's performance counts as "acceptable" at a load level when both hold:
- p95 completion time ≤ 1.5 × LocalFirst's p95 at the lowest load level, and
- failure rate ≤ 1%.

The headline result is: *"Adding Fog absorbs X% of EdgeOnly's overflow demand, and keeps performance acceptable up to load L\*."*

### 12.6 Host-busy experiment

1. Run a mixed trace at 1.5× load.
2. At t = 60 s, `experiments/stress.py` starts foreign CPU load on Fog (pinned to non-slot cores, about 80% host CPU) and stops it at t = 180 s.
3. Compare host-busy **off** vs **on**: p95 completion time, failure rate, jobs placed on Fog during the stress window, and the impact on Fog's `exec_s`.

### 12.7 Outputs

Each run writes to `experiments/results/<block>/<run_id>/`:
- `manifest.json`
- `jobs.csv`
- `decisions.csv`
- `telemetry.csv`
- `events.csv`

`analyze.py` regenerates every table and plot from these files alone.

**Machine-time budget:** about 4 strategies × 5 loads × 3 repetitions × roughly 5 minutes ≈ 5 h, plus the ablation (about 1 h). Run it unattended.

---

## 13. Milestones, checklists and acceptance criteria

Each milestone is merged and then tagged (`v1-m1`, …). Evidence goes in `docs/evidence/mN.md` and contains the commands run, their output and screenshots. Status words are PASS / FAIL / BLOCKED / NOT RUN.

### M0: Preserve the baseline

**Checklist**
- [ ] Tag `midterm-baseline`.
- [ ] Create branch `v1`.
- [ ] Add `pyproject.toml` with pytest config and pinned dependencies.
- [ ] Add a `conftest.py` so the legacy tests run from the repo root.
- [ ] Write `CLAUDE.md` (invariants from §3, commands, current milestone).
- [ ] Add `.env.example`.

**Acceptance criteria**
- AC-0.1: `pytest` from the repo root passes both legacy tests.
- AC-0.2: The tag exists; no legacy file has been modified.

### M1: Windows controller → Linux worker → real subprocess → result (highest priority)

**Checklist**
- [ ] `shared/` (states, catalog, schemas), with tests.
- [ ] `worker/workloads/cpu_hash`.
- [ ] Journal, plus a minimal supervisor: 1 slot, spawn, wait, result.
- [ ] Agent: register and heartbeat.
- [ ] Controller: SQLite, register, heartbeat, `POST /jobs` with `placement=<worker_id>`, `GET /jobs/{id}`.
- [ ] `setup.sh` (venv, `.env` prompts).
- [ ] **Network setup:** Windows firewall inbound rule for TCP 8000 (scoped to the LAN or Tailscale), controller bound to the reachable IP, and connectivity checked from Linux with `curl /api/v1/health`.

**Acceptance criteria**
- AC-1.1: The Linux worker registers, and the controller shows its **real hostname** and OS.
- AC-1.2: A `cpu_hash` job submitted on Windows reaches SUCCEEDED. The record shows the Linux hostname, PID, `exec_s`, `cpu_s` and the checksum.
- AC-1.3: That PID appears in `htop` on Linux while the job runs; a screenshot goes in the evidence.
- AC-1.4: Invalid parameters are rejected by the controller (422) and, in a separate test that bypasses it, by the worker (`FAILED(worker_validation)`).
- AC-1.5: Every transition the job went through is present in `job_events`.

### M2: Edge agent on Windows, plus every workload type

**Checklist**
- [ ] Agent runs on Windows as a separate process; `start.ps1` / `stop.ps1`, with PID files; `-Sim` mode.
- [ ] `data_digest` (blob store and input download, with measured transfer).
- [ ] `mem_touch` (hold included in `exec_s`, cancel checked during the hold).
- [ ] Multiple slots with core pinning; controller pinned away from the Edge slot cores.

**Acceptance criteria**
- AC-2.1: Both workers heartbeat. Freshness on the dashboard/API updates every second or so.
- AC-2.2: All three workload types succeed on both workers, and the PIDs are visible in Task Manager / `Get-Process` and in `htop`.
- AC-2.3: `data_digest` on Fog records `transfer_bytes` equal to `input_bytes` and `transfer_s` > 0. On Edge, the transfer happens over localhost.
- AC-2.4: For `mem_touch` with `hold_s=10`, `exec_s` ≥ 10 and `hold_s_actual` ≈ 10.

### M3: LocalFirst scheduling, pending queue and host-busy

**Checklist**
- [ ] `scheduler/` (views, eligibility, host-busy, policies), with exhaustive unit tests.
- [ ] Scheduler loop; FIFO-with-skip; aging; decision records.
- [ ] Derived reservations under the single writer.
- [ ] `PUT /scheduler`.
- [ ] Overflow flag.
- [ ] `experiments/stress.py`.

**Acceptance criteria**
- AC-3.1: Unit tests cover every eligibility reason, LocalFirst ordering and tie-breaks, structural reject, and the host-busy hysteresis (enter, exit, flapping input).
- AC-3.2: Under increasing load, jobs fill Edge, then spill to Fog, then stay PENDING. Every decision lists eligible and ineligible workers with reasons.
- AC-3.3: **No overbooking.** In a burst of 100 concurrent submissions, `job_events` shows no instant where a worker exceeds its slots or memory budget.
- AC-3.4: **Host-busy:** starting `stress.py` on Fog marks it `host_busy` within about 10 s, and new jobs avoid it. After the stress stops, Fog becomes eligible again. Both transitions appear as events.
- AC-3.5: A job too large for every worker's memory budget is REJECTED with per-worker reasons. A job too large only for Edge goes to Fog.

### M4: Reliability

**Checklist**
- [ ] Cancel (PENDING, ACCEPTED and RUNNING).
- [ ] `timeout_s` and `max_runtime_s`.
- [ ] RSS watchdog.
- [ ] Verified termination.
- [ ] Staleness sweep → UNKNOWN → reconciliation.
- [ ] Lost timeout and queue timeout.
- [ ] Duplicate `seq` / duplicate assignment handling.
- [ ] Agent restart recovery.
- [ ] Controller restart.

**Acceptance criteria**
- AC-4.1: Cancelling a RUNNING job → `KILLED(cancel)`, `termination_verified=true`, the OS shows no process, and `kill_latency_ms` is recorded. This holds on **both** Windows and Linux.
- AC-4.2: A `cpu_hash` that exceeds `timeout_s` → `KILLED(timeout)`, verified.
- AC-4.3: A test-mode over-allocation → `KILLED(memory_limit)`.
- AC-4.4: Unplugging Fog's network mid-job → the worker goes stale within 6 s and the job goes UNKNOWN. Reconnecting → the job reconciles to its true outcome. Meanwhile, new jobs went to Edge.
- AC-4.5: Duplicate heartbeat `seq` and duplicate assignment delivery are tested. Across all worker journals, no `job_id` ever has two spawns.
- AC-4.6: Killing the Fog agent mid-job and restarting it → the orphan is killed and verified, the job ends `FAILED(agent_restart)`, and no orphan processes remain.
- AC-4.7: Killing the controller mid-run and restarting it → jobs, events and decisions are intact, in-flight jobs reconcile, and nothing is duplicated.

### M5: Dashboard with real data

**Checklist**
- [ ] Workers, Jobs, Decisions, Overview and Submit views.
- [ ] Same-origin API; the build is served by the controller.
- [ ] Move the mid-term code to `legacy/`.

**Acceptance criteria**
- AC-5.1: Every value displayed traces back to an API field. A grep of `frontend/src` finds no hardcoded tiers, scores or metrics; reviewed.
- AC-5.2: A stale worker and a host-busy worker are both visibly marked.
- AC-5.3: The dashboard stays usable while Fog is disconnected.

### M6: Benchmarking

**Checklist**
- [ ] `gen_trace`, `replay`, `analyze`.
- [ ] Runs API and export.
- [ ] Reference timing run.
- [ ] Pilot run.
- [ ] Full sweep.
- [ ] Host-busy ablation.

**Acceptance criteria**
- AC-6.1: The same seed produces an identical trace hash, and replay submits the same sequence.
- AC-6.2: Every run directory has a manifest and CSVs, and `analyze.py` regenerates all tables and plots from them alone.
- AC-6.3: Results cover every strategy across the load sweep, with 3 repetitions each. The overflow metrics and the SLO verdict are reported. Conclusions only claim what the data shows.

### M7: Final demonstration (rehearsed twice on the real machines)

1. Normal submission → runs on Edge (PID visible in Task Manager).
2. Edge saturates → Fog takes the overflow (PID visible in `htop`).
3. Both saturate → backlog grows; the overflow counter shows the Cloud demand that would have occurred.
4. Stress on Fog → Fog goes `host_busy` → jobs avoid it → stress stops → Fog comes back.
5. Fog disconnected → stale and UNKNOWN → reconnect → reconcile.
6. Cancel a long job → verified termination.
7. Show the results charts: EdgeOnly vs LocalFirst overflow demand and p95 across load levels.

**Acceptance criteria**
- AC-7.1: The full demo checklist is executed twice without manual database fixes.
- AC-7.2: Three start/stop cycles on each machine leave no orphan processes and no port conflicts.

**Suggested timing (adjust to the evaluation date):** M0–M1 in week 1; M2–M3 in week 2; M4–M5 in week 3; M6–M7 in week 4. **M1 must pass before anything else continues.**

---

## 14. Risks and dependencies

| Risk | Impact | Mitigation |
|---|---|---|
| **Windows Defender Firewall** blocks inbound connections to `python.exe` on 8000 | M1 fails for a non-code reason | Explicit inbound rule scoped to the LAN or Tailscale subnet; `setup.ps1` prints the command; a doctor check |
| Network profile set to "Public" on Windows | The firewall is stricter | Set the profile to Private, or scope the rule to the Tailscale interface |
| LAN client isolation (campus or hotel Wi-Fi) | Linux can't reach Windows | Use Tailscale. Check `tailscale status` shows a **direct** connection, not a relay |
| Laptop sleep, lid-close suspend, Wi-Fi power saving | Workers disappear mid-experiment | Linux: `HandleLidSwitch=ignore` and Wi-Fi power save off. Windows: a power plan that never sleeps on AC. Documented in the setup |
| Edge shares a machine with the controller, dashboard and harness | Noise in Edge timings; the host-busy rule triggers on Edge | Pin the slots to dedicated cores, keep the browser and harness light, record Edge's foreign CPU |
| Thermal throttling on laptops | Run-to-run variance | Rotate strategy order, cool down between runs, 3 repetitions, record CPU frequency if available |
| Python version differences (old laptop) | Syntax or dependency failures | Support 3.10–3.12, pin dependencies, add a CI matrix later |
| psutil behaviour differs (Windows `cpu_percent` and working set) | Telemetry mismatch | Normalise to the whole host, label working set, unit-test the foreign-CPU calculation |
| Clock skew between machines | Wrong latencies | Use only controller-observed timestamps; workers report durations only |
| Paths with spaces (`JAGANNATH MUKHERJEE`) | Script failures | Quote every path; test `setup.ps1` from that path |
| Scope creep back into v2 features | Missed deadline | §2 is the contract; anything else needs owner approval |

---

## 15. References (verify all bibliographic metadata and DOIs before using them in the report)

1. J. McChesney et al., "DeFog: Fog Computing Benchmarks," SEC 2019.
2. D. Kimovski et al., "Cloud, Fog, or Edge: Where to Compute?," IEEE Internet Computing, 2021.
3. M. S. Aslanpour, S. S. Gill, A. N. Toosi, "Performance Evaluation Metrics for Cloud, Fog and Edge Computing…," Internet of Things, 2020.
4. S. Jeong et al., "Combined Autoscaling and Offloading for Efficient Resource Management in Fog Computing," HCIS, 2025. *(Not verified from memory; check before citing.)*
5. L. Cao et al., "Cost Optimization in Edge Computing: A Survey," Artificial Intelligence Review, 2024. *(Not verified from memory; check before citing.)*
6. The KEDA documentation (the Redis Streams scaler is relevant for V2), and the Kubernetes HPA documentation.

---

## 16. After V1 is complete: further setup needed

### 16.1 To run or demo V1 on a new day or machine (operational setup)

**Windows laptop (controller + Edge)**
- Install Python 3.10–3.12 and Node (Node is only needed to rebuild the dashboard).
- Run `setup.ps1`, which creates the venv, installs dependencies, builds the dashboard and writes `.env`.
- Add the firewall inbound rule for TCP 8000, scoped to the LAN or Tailscale.
- Power plan: never sleep on AC; keep the laptop plugged in during experiments.
- Optional: Tailscale, for use off a shared LAN.
- Start and stop with `start.ps1` / `stop.ps1`.

**Linux laptop (Fog)**
- Install Python and run `setup.sh` (venv, `.env`, systemd unit `dwr-worker`).
- Set `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf` and turn off Wi-Fi power saving.
- Optional: Tailscale.
- Check that `systemctl status dwr-worker` is active and the worker appears as fresh on the dashboard.

**Both machines**
- Keep worker tokens in `.env` only (never committed). Rotate them by editing `.env` on both sides.
- Back up `var/controller/dwr.db` and `experiments/results/` after experiment runs.
- Before any demo: run `doctor.py` and the M7 checklist.

### 16.2 To add the Cloud tier (V2; the architecture already allows it)

**1. Provision a Cloud VM**
- Choose a small Ubuntu VM. Check the provider's current free-tier or student offers.
- Set a **budget alert** and **auto-shutdown**.
- Firewall: allow inbound SSH only. Workers need no open ports.

**2. Connect it**
- Install Tailscale on the VM; **required**, because the VM cannot reach a home laptop behind NAT otherwise.
- Confirm a direct connection with `tailscale ping`.

**3. Deploy the worker**
- `git clone` the repo, then run `./scripts/setup.sh` with `DWR_TIER=cloud`.
- Systemd does the rest. **No code change is needed**: `cloud` is already tier rank 2 in `LocalFirst`.

**4. Evaluate**
- Add the `CloudOnly` policy (about 20 lines).
- Re-run the sweep, now with **real** Cloud slot-seconds, peak concurrent Cloud jobs and demand episodes, plus WAN transfer cost.
- Make a monetary claim only if the VM is billed per use.

### 16.3 To move dispatch to Redis Streams and KEDA (V2/V3)

| Step | Work |
|---|---|
| Redis | Run Redis 7 (with `appendonly yes` and a password or ACL), in Docker on the controller host or on an always-on small VM. Bind it to the Tailscale IP only |
| Streams | One stream per worker (`jobs:edge`, `jobs:fog`) plus `jobs:overflow` for Cloud. Workers use `XREADGROUP BLOCK`, and `XACK` only after the terminal result is stored. **Never** use `XAUTOCLAIM` for running jobs (it would cause duplicate execution) |
| Code | Replace the dispatch part of the heartbeat (assignment delivery) behind a `Dispatcher` interface. The journal, state machine, SQLite records and liveness stay the same. Add a startup reconciliation between SQLite and the streams |
| Kubernetes | Install k3s on the Cloud VM. Containerise the worker agent (Dockerfile plus a registry) |
| KEDA | Install KEDA. Use a `ScaledObject` with the **Redis Streams scaler** on `jobs:overflow` (lag- or pending-based), scaling Cloud workers from 0 to N. Local devices take the load first; Cloud replicas exist only while there is overflow. This is the original motivation, implemented directly |

### 16.4 To iterate the scheduler (later versions)

Every option below plugs into the same `Scheduler` interface (§7.1), so no architectural change is needed:
- `LeastLoaded`.
- The EFT model from `PROJECT_PLAN_v2.md` §4 (calibration, transfer-aware completion time, deadlines).
- Cost-aware scoring once Cloud billing is real.
- Compare each new policy against `LocalFirst` using the same V1 harness and traces.

### 16.5 Hardening (when needed)

- GitHub CI: pytest on Windows and Linux, plus the frontend build.
- Hard resource limits: cgroups v2 on Linux, Job Objects on Windows.
- TLS beyond Tailscale; token rotation tooling.
- Re-adopting orphaned processes after an agent restart.
- Long-poll heartbeats if assignment latency matters.
