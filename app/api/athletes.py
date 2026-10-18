from fastapi import APIRouter, HTTPException

from app.models import Athlete, PhysiologicalSample, TrackingSample
from app.store.physiological import latest_physiological_store
from app.store.tracking import latest_tracking_store

router = APIRouter(prefix="/athletes", tags=["athletes"])


@router.post("/validate", response_model=Athlete)
def validate_athlete(athlete: Athlete) -> Athlete:
    """Validate an athlete payload and echo it back. No persistence yet."""
    return athlete


@router.get("/{athlete_id}/physiological/latest", response_model=PhysiologicalSample)
def get_latest_physiological(athlete_id: str) -> PhysiologicalSample:
    """Return the most recent physiological sample received for an athlete."""
    sample = latest_physiological_store.get(athlete_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No physiological sample for athlete '{athlete_id}'")
    return sample


@router.get("/{athlete_id}/tracking/latest", response_model=TrackingSample)
def get_latest_tracking(athlete_id: str) -> TrackingSample:
    """Return the most recent tracking sample received for an athlete."""
    sample = latest_tracking_store.get(athlete_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No tracking sample for athlete '{athlete_id}'")
    return sample
