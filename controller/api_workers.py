"""Worker-facing endpoints (plan §8.1). Input download arrives in M2."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

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
