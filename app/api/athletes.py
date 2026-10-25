from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import AwareDatetime

from app.core import clock
from app.core.config import get_settings
from app.core.summary import summarize_window
from app.models import Athlete, AthleteSummary, AthleteWindow, PhysiologicalSample, TrackingRules, TrackingSample
from app.store.athletes import athlete_registry
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.physiological import latest_physiological_store, physiological_history_store
from app.store.tracking import latest_tracking_store, tracking_history_store

router = APIRouter(prefix="/athletes", tags=["athletes"])

# Longest window that may be requested, in seconds. Matches the history cap at one sample per second.
MAX_WINDOW_SECONDS = 300


# ---- registry --------------------------------------------------------------

@router.post("", response_model=Athlete, status_code=status.HTTP_201_CREATED)
def register_athlete(athlete: Athlete) -> Athlete:
    """Register an athlete. 409 if the id is already registered (existing record is kept)."""
    if not athlete_registry.add(athlete):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Athlete '{athlete.id}' is already registered")
    return athlete


@router.get("", response_model=list[Athlete])
def list_athletes() -> list[Athlete]:
    """All registered athletes sorted by id; empty list if none."""
    return athlete_registry.list()


@router.post("/validate", response_model=Athlete)
def validate_athlete(athlete: Athlete) -> Athlete:
    """Validate an athlete payload and echo it back without registering it."""
    return athlete


@router.get("/{athlete_id}", response_model=Athlete)
def get_athlete(athlete_id: str) -> Athlete:
    """Return a registered athlete, or 404."""
    athlete = athlete_registry.get(athlete_id)
    if athlete is None:
        raise HTTPException(status_code=404, detail=f"Athlete '{athlete_id}' is not registered")
    return athlete


# ---- latest samples (registration not required) ----------------------------

@router.get("/{athlete_id}/physiological/latest", response_model=PhysiologicalSample)
def get_latest_physiological(athlete_id: str) -> PhysiologicalSample:
    """Return the most recent physiological sample received for an athlete."""
    sample = latest_physiological_store.get(athlete_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No physiological sample for athlete '{athlete_id}'")
    return sample


@router.get("/{athlete_id}/physiological/history", response_model=list[PhysiologicalSample])
def get_physiological_history(
    athlete_id: str,
    limit: int = Query(60, ge=1, le=MAX_HISTORY_SAMPLES, description="Newest samples to return"),
) -> list[PhysiologicalSample]:
    """The newest `limit` physiological samples for an athlete, oldest to newest. Empty list if none."""
    return physiological_history_store.recent(athlete_id, limit)


@router.get("/{athlete_id}/tracking/latest", response_model=TrackingSample)
def get_latest_tracking(athlete_id: str) -> TrackingSample:
    """Return the most recent tracking sample received for an athlete."""
    sample = latest_tracking_store.get(athlete_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No tracking sample for athlete '{athlete_id}'")
    return sample


@router.get("/{athlete_id}/tracking/history", response_model=list[TrackingSample])
def get_tracking_history(
    athlete_id: str,
    limit: int = Query(60, ge=1, le=MAX_HISTORY_SAMPLES, description="Newest samples to return"),
) -> list[TrackingSample]:
    """The newest `limit` tracking samples for an athlete, oldest to newest. Empty list if none."""
    return tracking_history_store.recent(athlete_id, limit)


WINDOW_SECONDS = Query(60, ge=1, le=MAX_WINDOW_SECONDS, description="Window length ending at `end`, or at backend time now when `end` is omitted")
WINDOW_END = Query(
    None,
    description=(
        "Recorded review: the exact window end as an ISO 8601 timestamp with timezone, e.g. 2026-10-20T10:00:02Z "
        "(encode '+' as %2B). Must not be after backend time. Omitted: live mode, backend time now."
    ),
)


def resolve_window_end(end: datetime | None) -> datetime:
    """The window end for one request: backend time now (live), or the requested recorded instant.

    Backend time is read exactly once either way. A recorded end after backend
    time is rejected, so a review never includes samples that could still
    arrive within the ingestion clock-skew allowance.
    """
    now = clock.utc_now()
    if end is None:
        return now
    if end > now:
        raise HTTPException(
            status_code=422,
            detail=f"end {end.isoformat()} is after backend time {now.isoformat()}; a recorded review cannot end in the future",
        )
    return end


def select_window(athlete_id: str, seconds: int, min_confidence: float, end: datetime | None = None) -> AthleteWindow:
    """Both streams recorded in the last `seconds` before one window end.

    Live (`end` is None): the end is backend time, read once; samples timestamped
    after it are excluded even if ingestion accepted them within the clock-skew
    tolerance. Recorded review (`end` given, timezone-aware, not in the future):
    that exact instant is the end and the start is `seconds` before it. The
    selection, inclusive boundaries, ordering and `tracking_rules` are the same in
    both cases; only the retained samples (newest 300 per athlete per stream) can
    be reviewed. Reads the stores directly. `min_confidence` is the configured
    DISTANCE_MIN_CONFIDENCE, echoed in `tracking_rules` so the response is
    self-contained.
    """
    window_end = resolve_window_end(end)
    window_start = window_end - timedelta(seconds=seconds)
    return AthleteWindow(
        athlete_id=athlete_id,
        window_start=window_start,
        window_end=window_end,
        physiological_samples=physiological_history_store.between(athlete_id, window_start, window_end),
        tracking_samples=tracking_history_store.between(athlete_id, window_start, window_end),
        tracking_rules=TrackingRules(min_confidence=min_confidence),
    )


@router.get("/{athlete_id}/window", response_model=AthleteWindow)
def get_athlete_window(athlete_id: str, seconds: int = WINDOW_SECONDS, end: AwareDatetime | None = WINDOW_END) -> AthleteWindow:
    """Physiological and tracking samples recorded in the `seconds` before `end` (default: now), oldest to newest, with the applied tracking rules."""
    return select_window(athlete_id, seconds, get_settings().distance_min_confidence, end)


@router.get("/{athlete_id}/summary", response_model=AthleteSummary)
def get_athlete_summary(athlete_id: str, seconds: int = WINDOW_SECONDS, end: AwareDatetime | None = WINDOW_END) -> AthleteSummary:
    """Descriptive counts, heart-rate mean/peak, peak speed, gap-aware distance and high-speed estimates, and timestamps for the `seconds` before `end` (default: now).

    Sample ages are always relative to `window_end`: in a recorded review they mean seconds before the selected end, not age relative to the present.
    """
    settings = get_settings()
    return summarize_window(
        select_window(athlete_id, seconds, settings.distance_min_confidence, end),
        distance_max_gap_seconds=settings.distance_max_gap_seconds,
        distance_min_confidence=settings.distance_min_confidence,
        high_speed_threshold_mps=settings.high_speed_threshold_mps,
    )
