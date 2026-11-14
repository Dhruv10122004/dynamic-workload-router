"""Job endpoints (plan §8.2): submit, list, get. Cancel arrives in M4."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from controller.auth import require_admin
from shared.schemas import JobSubmit

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_admin)])


@router.post("/jobs", status_code=202)
async def submit(sub: JobSubmit, request: Request):
    return await request.app.state.service.submit(sub)


@router.get("/jobs")
async def list_jobs(request: Request, state: str | None = None, worker: str | None = None,
                    run_id: str | None = None, limit: int = Query(100, ge=1, le=5000), after: str | None = None):
    return request.app.state.service.list_jobs(state=state, worker=worker, run_id=run_id, limit=limit, after=after)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    return request.app.state.service.get_job(job_id)
