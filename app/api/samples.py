from fastapi import APIRouter

from app.api.timestamps import reject_future_timestamp
from app.models import PhysiologicalSample, TrackingSample
from app.store.physiological import latest_physiological_store, physiological_history_store
from app.store.tracking import latest_tracking_store, tracking_history_store

router = APIRouter(prefix="/samples", tags=["samples"])


@router.post("/physiological", response_model=PhysiologicalSample)
def ingest_physiological(sample: PhysiologicalSample) -> PhysiologicalSample:
    """Validate a physiological sample, record it (latest + history), and echo it back."""
    reject_future_timestamp(sample.timestamp)
    latest_physiological_store.upsert(sample)
    physiological_history_store.add(sample)
    return sample


@router.post("/tracking", response_model=TrackingSample)
def ingest_tracking(sample: TrackingSample) -> TrackingSample:
    """Validate a tracking sample, record it (latest + history), and echo it back."""
    reject_future_timestamp(sample.timestamp)
    latest_tracking_store.upsert(sample)
    tracking_history_store.add(sample)
    return sample
