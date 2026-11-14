"""Worker-facing endpoints (plan §8.1)."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse

from controller.auth import bearer_token, check_worker_token, require_worker
from shared.schemas import Heartbeat, HeartbeatReply, RegisterReply, RegisterRequest

router = APIRouter(tags=["workers"])


@router.post("/workers/register", response_model=RegisterReply)
async def register(req: RegisterRequest, request: Request, authorization: str | None = Header(None)):
    token = bearer_token(authorization)
    check_worker_token(request, req.worker_id, token)
    return await request.app.state.service.register(req, token)


@router.post("/workers/{worker_id}/heartbeat", response_model=HeartbeatReply)
async def heartbeat(hb: Heartbeat, request: Request, worker_id: str = Depends(require_worker)):
    return await request.app.state.service.heartbeat(worker_id, hb)


@router.get("/jobs/{job_id}/input")
async def job_input(job_id: str, request: Request, authorization: str | None = Header(None)):
    """Raw bytes with Content-Length, no compression; only the assigned worker may fetch."""
    token = bearer_token(authorization)
    if not any(hmac.compare_digest(t.encode(), token.encode())
               for t in request.app.state.settings.worker_tokens.values()):
        raise HTTPException(401, "unknown worker or wrong token")  # before revealing whether the job exists
    path = request.app.state.service.job_input(job_id, lambda wid: check_worker_token(request, wid, token))
    return FileResponse(path, media_type="application/octet-stream")
