from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.core import clock
from app.models import PhysiologicalSample, TrackingSample
from app.store.physiological import latest_physiological_store
from app.store.tracking import latest_tracking_store

router = APIRouter(prefix="/samples", tags=["samples"])


def _reject_future_timestamp(timestamp: datetime) -> None:
    """Raise 422 if the sample timestamp is further ahead of backend time than allowed."""
    error = clock.future_skew_error(timestamp)
    if error is not None:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", "timestamp"], "msg": error, "type": "value_error.timestamp_in_future"}],
        )


@router.post("/physiological", response_model=PhysiologicalSample)
def ingest_physiological(sample: PhysiologicalSample) -> PhysiologicalSample:
    """Validate a physiological sample, record it as the athlete's latest if newest, and echo it back."""
    _reject_future_timestamp(sample.timestamp)
    latest_physiological_store.upsert(sample)
    return sample


@router.post("/tracking", response_model=TrackingSample)
def ingest_tracking(sample: TrackingSample) -> TrackingSample:
    """Validate a tracking sample, record it as the athlete's latest if newest, and echo it back."""
    _reject_future_timestamp(sample.timestamp)
    latest_tracking_store.upsert(sample)
    return sample
