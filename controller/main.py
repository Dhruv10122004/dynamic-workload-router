"""App factory (plan §3, §8, §10): /api/v1 routers, the built dashboard on the same
origin, restricted CORS (only the Vite dev server), error mapping."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from controller import api_admin, api_jobs, api_workers
from controller.db import Database
from controller.playground import Playground
from controller.service import ControllerService, ServiceError
from controller.settings import Settings
from shared.schemas import SCHEMA_VERSION
from shared.states import IllegalTransition

API_PREFIX = "/api/v1"


def create_app(settings: Settings) -> FastAPI:
    db = Database(settings.db_path)
    service = ControllerService(settings, db)
    playground = Playground(service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        await playground.recover()
        try:
            yield
        finally:
            playground.shutdown()
            await service.stop()

    app = FastAPI(title="DWR controller", version=f"schema-{SCHEMA_VERSION}", lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.state.playground = playground
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["*"], allow_headers=["*"])
    for r in (api_admin.public, api_admin.router, api_jobs.router, api_workers.router):
        app.include_router(r, prefix=API_PREFIX)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, e: ServiceError):
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    @app.exception_handler(IllegalTransition)
    async def _illegal(_: Request, e: IllegalTransition):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_unavailable(_: Request, e: sqlite3.OperationalError):
        return JSONResponse({"detail": f"database unavailable: {e}"}, status_code=503)

    # Mounted last so every /api/v1 route wins. Without a build the API still works.
    dist = settings.frontend_dist
    if dist is not None and (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")

    return app
