"""UI/admin endpoints (plan §8.2): health, catalog, workers, scheduler, events."""
from __future__ import annotations

import psutil
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from controller.auth import require_admin
from shared.catalog import CATALOG

public = APIRouter(tags=["health"])
router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


class SchedulerUpdate(BaseModel):
    """PUT /scheduler body: any subset of the active settings."""
    model_config = ConfigDict(extra="forbid")
    policy: str | None = None
    host_busy_enabled: bool | None = None
    host_busy_high: float | None = None
    host_busy_low: float | None = None
    aging_s: float | None = None
    queue_timeout_s: float | None = None
@public.get("/health")
async def health(request: Request):
    """Unauthenticated so a worker host can check reachability with curl (DECISIONS D-008)."""
    service = request.app.state.service
    try:
        service.db.one("SELECT 1")
    except Exception as e:
        return JSONResponse({"status": "NOT_READY", "reasons": [f"database: {e}"]}, status_code=503)
    workers = service.db.all("SELECT worker_id FROM workers")
    fresh = [w["worker_id"] for w in workers if service.is_fresh(w["worker_id"])]
    reasons = [] if fresh else ["no fresh worker"]
    try:
        affinity = psutil.Process().cpu_affinity()
    except (psutil.Error, AttributeError):
        affinity = None
    return {"status": "READY" if fresh else "DEGRADED", "reasons": reasons, "fresh_workers": fresh,
            "controller_cpus": affinity}


@router.get("/catalog")
async def catalog():
    return [wt.to_json() for wt in CATALOG.values()]


@router.get("/workers")
async def list_workers(request: Request):
    return request.app.state.service.list_workers()


@router.get("/workers/{worker_id}")
async def get_worker(worker_id: str, request: Request):
    return request.app.state.service.get_worker(worker_id)


@router.post("/workers/{worker_id}/drain")
async def drain_worker(worker_id: str, request: Request):
    """No new assignments; running jobs finish (plan §8.2)."""
    return await request.app.state.service.set_draining(worker_id, True)


@router.post("/workers/{worker_id}/enable")
async def enable_worker(worker_id: str, request: Request):
    return await request.app.state.service.set_draining(worker_id, False)


@router.get("/scheduler")
async def get_scheduler(request: Request):
    return request.app.state.service.get_scheduler()


@router.put("/scheduler")
async def put_scheduler(body: SchedulerUpdate, request: Request):
    """409 while any job is non-terminal (plan §7.3)."""
    return await request.app.state.service.put_scheduler(body.model_dump(exclude_none=True))


@router.get("/events")
async def list_events(request: Request, after: int = 0, kind: str | None = None,
                      limit: int = Query(500, ge=1, le=5000)):
    return request.app.state.service.list_events(after=after, limit=limit, kind=kind)
