from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    return app


app = create_app()
