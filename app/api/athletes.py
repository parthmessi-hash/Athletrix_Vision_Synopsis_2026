from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, status

from app.core import clock
from app.core.config import get_settings
from app.core.summary import summarize_window
from app.models import Athlete, AthleteSummary, AthleteWindow, PhysiologicalSample, TrackingSample
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


WINDOW_SECONDS = Query(60, ge=1, le=MAX_WINDOW_SECONDS, description="Window length ending at backend time now")


def select_window(athlete_id: str, seconds: int) -> AthleteWindow:
    """Both streams recorded in the last `seconds`, cut at one captured backend instant.

    Backend time is read once; samples timestamped after it are excluded even if
    ingestion accepted them within the clock-skew tolerance. Reads the stores
    directly.
    """
    window_end = clock.utc_now()
    window_start = window_end - timedelta(seconds=seconds)
    return AthleteWindow(
        athlete_id=athlete_id,
        window_start=window_start,
        window_end=window_end,
        physiological_samples=physiological_history_store.between(athlete_id, window_start, window_end),
        tracking_samples=tracking_history_store.between(athlete_id, window_start, window_end),
    )


@router.get("/{athlete_id}/window", response_model=AthleteWindow)
def get_athlete_window(athlete_id: str, seconds: int = WINDOW_SECONDS) -> AthleteWindow:
    """Physiological and tracking samples recorded in the last `seconds`, oldest to newest."""
    return select_window(athlete_id, seconds)


@router.get("/{athlete_id}/summary", response_model=AthleteSummary)
def get_athlete_summary(athlete_id: str, seconds: int = WINDOW_SECONDS) -> AthleteSummary:
    """Descriptive counts, heart-rate mean/peak, peak speed, gap-aware distance and high-speed estimates, and timestamps for the last `seconds`."""
    settings = get_settings()
    return summarize_window(
        select_window(athlete_id, seconds),
        distance_max_gap_seconds=settings.distance_max_gap_seconds,
        distance_min_confidence=settings.distance_min_confidence,
        high_speed_threshold_mps=settings.high_speed_threshold_mps,
    )
