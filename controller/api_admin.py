"""UI/admin endpoints (plan §8.2). M1 subset: health, catalog, workers."""
from __future__ import annotations

import psutil
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from controller.auth import require_admin
from shared.catalog import CATALOG

public = APIRouter(tags=["health"])
router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


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
