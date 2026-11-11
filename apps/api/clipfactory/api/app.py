"""FastAPI application. Requests only read and enqueue; all heavy work happens in the worker."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from clipfactory.api import routes_clips, routes_projects, routes_system
from clipfactory.config import get_settings
from clipfactory.db import migrate
from clipfactory.errors import ClipFactoryError
from clipfactory.logs import get_logger, redact, register_secret, setup_logging

log = get_logger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN201
    settings = get_settings()
    setup_logging(settings.clipfactory_log_level)
    if settings.openrouter_api_key:
        register_secret(settings.openrouter_api_key.get_secret_value())
    migrate()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="CLIPFACTORY", version="0.1.0", lifespan=lifespan)
    # The dashboard talks to the API directly (large uploads, ranged video playback).
    # Only local origins are allowed; the API itself binds to 127.0.0.1.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    @app.exception_handler(ClipFactoryError)
    async def _typed(_request: Request, exc: ClipFactoryError) -> JSONResponse:
        payload = exc.to_dict()
        payload["message"] = redact(payload["message"])
        payload["details"] = {k: redact(str(v))[:1000] for k, v in payload["details"].items()}
        return JSONResponse(status_code=exc.http_status, content={"error": payload})

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Erreur interne — voir les logs de l'API.", "retryable": False, "details": {}}})

    app.include_router(routes_system.router, prefix="/api")
    app.include_router(routes_projects.router, prefix="/api")
    app.include_router(routes_clips.router, prefix="/api")
    return app


app = create_app()
