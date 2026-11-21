"""Auth dependencies (plan §8): worker bearer tokens; admin = localhost or X-Admin-Token."""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1"})


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization[len("Bearer "):].strip()


def check_worker_token(request: Request, worker_id: str, token: str) -> None:
    expected = request.app.state.settings.worker_tokens.get(worker_id)
    if expected is None or not hmac.compare_digest(expected.encode(), token.encode()):
        raise HTTPException(401, "unknown worker or wrong token")


def require_worker(request: Request, worker_id: str, authorization: str | None = Header(None)) -> str:
    check_worker_token(request, worker_id, bearer_token(authorization))
    return worker_id


def require_admin(request: Request, x_admin_token: str | None = Header(None)) -> None:
    if request.client is not None and request.client.host in LOCAL_HOSTS:
        return
    expected = request.app.state.settings.admin_token
    if expected and x_admin_token and hmac.compare_digest(expected.encode(), x_admin_token.encode()):
        return
    raise HTTPException(401, "admin endpoints need a localhost client or X-Admin-Token")
