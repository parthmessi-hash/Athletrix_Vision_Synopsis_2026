from fastapi import APIRouter

from app.api.timestamps import reject_future_timestamp
from app.core.ingest import ingest
from app.models import PhysiologicalSample, TrackingSample
from app.store.physiological import physiological_store
from app.store.tracking import tracking_store

router = APIRouter(prefix="/samples", tags=["samples"])


@router.post("/physiological", response_model=PhysiologicalSample)
def ingest_physiological(sample: PhysiologicalSample) -> PhysiologicalSample:
    """Validate a physiological sample, persist it (history + latest, plus the active recording if it covers the athlete, in one transaction), and echo it back."""
    reject_future_timestamp(sample.timestamp)
    ingest(physiological_store, "physiological", sample)
    return sample


@router.post("/tracking", response_model=TrackingSample)
def ingest_tracking(sample: TrackingSample) -> TrackingSample:
    """Validate a tracking sample, persist it (history + latest, plus the active recording if it covers the athlete, in one transaction), and echo it back."""
    reject_future_timestamp(sample.timestamp)
    ingest(tracking_store, "tracking", sample)
    return sample
