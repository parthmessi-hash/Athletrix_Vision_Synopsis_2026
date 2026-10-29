from fastapi import APIRouter

from app.core import clock
from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
    }


@router.get("/clock")
def backend_clock() -> dict:
    """This engine's UTC wall clock now, to microseconds, for clock-offset estimates (developer timing).

    A client reads its own clock before (t0) and after (t1) the request: offset ~= backend_time - (t0 + t1) / 2, with an
    uncertainty of +/- (t1 - t0) / 2 if the request and response legs are not symmetric. Not a time service.
    """
    return {"backend_time": clock.utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")}
