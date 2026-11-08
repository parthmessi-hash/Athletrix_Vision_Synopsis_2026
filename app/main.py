from contextlib import asynccontextmanager

import json

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.timing import TimingMiddleware
from app.db import get_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_database()  # open the SQLite file and create tables if needed, before serving requests
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    # Allow the separate Athletrix_Web frontend to call this API from the browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Outermost: receipt time and Server-Timing for every request (see app/core/timing.py).
    app.add_middleware(TimingMiddleware)
    app.include_router(api_router, prefix="/api/v1")

    @app.exception_handler(RequestValidationError)
    async def _log_rejected_samples(request: Request, exc: RequestValidationError):
        """Record a rejected sample write (stream, athlete if the body named one, first error), then answer as FastAPI does."""
        path = request.url.path
        if request.method == "POST" and path.startswith("/api/v1/samples/"):
            try:
                body = json.loads(await request.body() or b"null")
                athlete = body.get("athlete_id") if isinstance(body, dict) and isinstance(body.get("athlete_id"), str) else None
            except (ValueError, UnicodeDecodeError):
                athlete = None
            err = exc.errors()[0] if exc.errors() else {}
            from app.core.timing import received_at  # noqa: PLC0415
            from app.store.rejections import rejection_store  # noqa: PLC0415
            try:
                rejection_store.add(received_at(request), path.rsplit("/", 1)[-1], athlete,
                                    f"invalid: {'.'.join(str(x) for x in err.get('loc', [])[1:])} {err.get('msg', '')}".strip())
            except Exception:  # noqa: BLE001  logging a rejection must never change the answer
                pass
        return await request_validation_exception_handler(request, exc)

    return app


app = create_app()
