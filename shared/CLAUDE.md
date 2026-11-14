# shared/

Code used by both controller and worker: job state machine, workload catalog,
wire schemas and job validation. Implements plan §4, §6.2, §8 (schemas).

## Public interface
- `states.py`: `JobState`, `TERMINAL`, `NON_TERMINAL`, `COMMITTED`, `WORKER_REPORTED`,
  `FAIL_REASONS`, `KILL_REASONS`, `can_transition(src, dst)`,
  `transition(src, dst, fail_reason=, kill_reason=) -> JobState` (raises `IllegalTransition`).
  FAILED needs a fail_reason legal for the source state; KILLED needs a kill_reason.
- `catalog.py`: `CATALOG[type] -> WorkloadType` with `params` (bounds/defaults),
  `declared_mem_mb(params)`, `max_runtime_s(params)`, `to_json()`.
- `schemas.py`: `SCHEMA_VERSION`; `validate_job(type, params, timeout_s, test_mode=)`
  -> `ValidatedJob` (raises `JobValidationError` with `.errors`); `validate_assignment(a)`
  (worker side). Pydantic wire models: `JobSubmit`, `RegisterRequest/Reply`, `Heartbeat`,
  `Telemetry`, `JobSnapshot`, `Assignment`, `HeartbeatReply`.
- `envfile.py`: `load_env(path | "-" | None) -> dict` (real `DWR_*` env vars win).

## Rules
- Pure: no network, no database, no filesystem writes (envfile only reads).
- Parameter bounds live only in catalog.py; schemas build their models from it.
- Bump `SCHEMA_VERSION` on any incompatible wire change (controller answers 409).
- The transition table must match plan §6.2 exactly; tests/unit/test_states.py holds an
  independent copy of it and checks all 10×10 pairs.

## Test
`.\.venv\Scripts\python -m pytest tests/unit/test_states.py tests/unit/test_schemas.py`
