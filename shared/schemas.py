"""Wire schemas (plan §8) and job validation (plan §4, invariant 3).

`validate_job()` is called by the controller on submit and `validate_assignment()`
by the worker before accepting an assignment, so parameters are checked twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from shared.catalog import CATALOG, WorkloadType

SCHEMA_VERSION = 1


# ---------------------------------------------------------------- job validation

def _params_model(wt: WorkloadType) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for s in wt.params:
        if s.test_only:
            fields[s.name] = (s.kind | None, Field(default=None, ge=s.min, le=s.max))
        else:
            fields[s.name] = (s.kind, Field(default=s.default, ge=s.min, le=s.max))
    return create_model(
        f"{wt.name}_params", __config__=ConfigDict(extra="forbid", strict=True), **fields
    )


PARAMS_MODELS: dict[str, type[BaseModel]] = {name: _params_model(wt) for name, wt in CATALOG.items()}


class JobValidationError(ValueError):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors))


@dataclass(frozen=True)
class ValidatedJob:
    type: str
    params: dict
    mem_mb: int
    max_runtime_s: float
    timeout_s: float | None


def validate_job(type_: str, params: dict | None, timeout_s: float | None = None,
                 *, test_mode: bool = False) -> ValidatedJob:
    wt = CATALOG.get(type_)
    if wt is None:
        raise JobValidationError([{"loc": ("type",), "msg": f"unknown workload type {type_!r}"}])
    if not isinstance(params, dict):
        raise JobValidationError([{"loc": ("params",), "msg": "params must be an object"}])
    try:
        p = PARAMS_MODELS[type_].model_validate(params).model_dump(exclude_none=True)
    except ValidationError as e:
        raise JobValidationError(
            [{"loc": ("params", *e_["loc"]), "msg": e_["msg"]} for e_ in e.errors()]
        ) from None
    errors = []
    for s in wt.params:
        if s.test_only and s.name in p and not test_mode:
            errors.append({"loc": ("params", s.name), "msg": "test-only parameter (needs DWR_TEST_MODE=1)"})
    max_runtime_s = wt.max_runtime_s(p)
    if timeout_s is not None and not (0 < timeout_s <= max_runtime_s):
        errors.append({"loc": ("timeout_s",), "msg": f"must be in (0, {max_runtime_s}]"})
    if errors:
        raise JobValidationError(errors)
    return ValidatedJob(type_, p, wt.declared_mem_mb(p), max_runtime_s, timeout_s)


def validate_assignment(a: "Assignment") -> ValidatedJob:
    """Worker-side re-validation. Test-only params were already gated by the controller."""
    v = validate_job(a.type, a.params, a.timeout_s, test_mode=True)
    errors = []
    if a.mem_mb != v.mem_mb:
        errors.append({"loc": ("mem_mb",), "msg": f"declared {v.mem_mb}, assignment says {a.mem_mb}"})
    if not (0 < a.max_runtime_s <= v.max_runtime_s):
        errors.append({"loc": ("max_runtime_s",), "msg": f"must be in (0, {v.max_runtime_s}]"})
    if a.timeout_s is not None and a.timeout_s > a.max_runtime_s:
        errors.append({"loc": ("timeout_s",), "msg": "exceeds max_runtime_s"})
    expected_input = v.params.get("input_bytes", 0)
    if a.input_bytes != expected_input:
        errors.append({"loc": ("input_bytes",), "msg": f"params say {expected_input}, assignment says {a.input_bytes}"})
    elif expected_input and not (a.input_url and a.input_sha256):
        errors.append({"loc": ("input_url",), "msg": "input_url and input_sha256 are required for an input"})
    if errors:
        raise JobValidationError(errors)
    return v


# ---------------------------------------------------------------- UI / harness

class JobSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    params: dict = Field(default_factory=dict)
    timeout_s: float | None = None
    placement: str = "auto"
    run_id: str | None = None
    trace_index: int | None = None


# ---------------------------------------------------------------- worker-facing

Tier = Literal["edge", "fog", "cloud"]


class SlotInfo(BaseModel):
    slot: int
    cpus: list[int]


class Capabilities(BaseModel):
    affinity: bool = True
    cpu_quota: str = "not_enforced"
    rss_watchdog: bool = False
    memory_hard_limit: str = "not_enforced"


class RegisterRequest(BaseModel):
    schema_version: int
    worker_id: str
    tier: Tier
    hostname: str
    os: str
    os_version: str
    python: str
    cpu_logical: int
    mem_total_mb: int
    slots: list[SlotInfo]
    mem_budget_mb: int
    mem_floor_mb: int
    supported_types: list[str]
    capabilities: Capabilities
    agent_version: str
    agent_instance_id: str


class RegisterReply(BaseModel):
    schema_version: int = SCHEMA_VERSION
    accepted: bool
    heartbeat_interval_s: float
    stale_after_s: float


class Telemetry(BaseModel):
    cpu_pct: float
    own_cpu_pct: float
    foreign_cpu_pct: float
    mem_total_mb: int
    mem_available_mb: int
    free_slots: int
    rtt_ms_last: float | None = None


class JobSnapshot(BaseModel):
    job_id: str
    state: Literal["ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED", "KILLED", "CANCELLED"]
    slot: int | None = None
    pid: int | None = None
    pid_create_time: float | None = None
    transfer_bytes: int | None = None
    transfer_s: float | None = None
    spawn_s: float | None = None
    exit_code: int | None = None
    result: dict | None = None
    error: str | None = None
    fail_reason: str | None = None
    kill_reason: str | None = None
    termination_verified: bool | None = None
    kill_latency_ms: float | None = None


class Heartbeat(BaseModel):
    schema_version: int
    agent_instance_id: str
    seq: int
    telemetry: Telemetry
    jobs: list[JobSnapshot] = Field(default_factory=list)


class Assignment(BaseModel):
    job_id: str
    type: str
    params: dict
    input_bytes: int = 0
    input_sha256: str | None = None
    input_url: str | None = None
    max_runtime_s: float
    timeout_s: float | None = None
    mem_mb: int


class HeartbeatReply(BaseModel):
    schema_version: int = SCHEMA_VERSION
    seq_ack: int
    server_processing_ms: float
    drain: bool
    assignments: list[Assignment] = Field(default_factory=list)
    cancel: list[str] = Field(default_factory=list)
    ack_terminal: list[str] = Field(default_factory=list)
