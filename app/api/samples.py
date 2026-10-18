from fastapi import APIRouter

from app.models import PhysiologicalSample, TrackingSample
from app.store.physiological import latest_physiological_store
from app.store.tracking import latest_tracking_store

router = APIRouter(prefix="/samples", tags=["samples"])


@router.post("/physiological", response_model=PhysiologicalSample)
def ingest_physiological(sample: PhysiologicalSample) -> PhysiologicalSample:
    """Validate a physiological sample, record it as the athlete's latest if newest, and echo it back."""
    latest_physiological_store.upsert(sample)
    return sample


@router.post("/tracking", response_model=TrackingSample)
def ingest_tracking(sample: TrackingSample) -> TrackingSample:
    """Validate a tracking sample, record it as the athlete's latest if newest, and echo it back."""
    latest_tracking_store.upsert(sample)
    return sample
