from fastapi import APIRouter

from app.api.timestamps import reject_future_timestamp
from app.models import PhysiologicalSample, TrackingSample
from app.store.physiological import physiological_store
from app.store.tracking import tracking_store

router = APIRouter(prefix="/samples", tags=["samples"])


@router.post("/physiological", response_model=PhysiologicalSample)
def ingest_physiological(sample: PhysiologicalSample) -> PhysiologicalSample:
    """Validate a physiological sample, persist it (history + latest in one transaction), and echo it back."""
    reject_future_timestamp(sample.timestamp)
    physiological_store.add(sample)
    return sample


@router.post("/tracking", response_model=TrackingSample)
def ingest_tracking(sample: TrackingSample) -> TrackingSample:
    """Validate a tracking sample, persist it (history + latest in one transaction), and echo it back."""
    reject_future_timestamp(sample.timestamp)
    tracking_store.add(sample)
    return sample
