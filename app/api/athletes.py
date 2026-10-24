from fastapi import APIRouter, HTTPException, Query, status

from app.models import Athlete, PhysiologicalSample, TrackingSample
from app.store.athletes import athlete_registry
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.physiological import latest_physiological_store, physiological_history_store
from app.store.tracking import latest_tracking_store

router = APIRouter(prefix="/athletes", tags=["athletes"])


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
