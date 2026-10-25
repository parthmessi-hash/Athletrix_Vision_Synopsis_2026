"""TrackingSample storage: one SQLite-backed store serves both latest-sample and history reads."""

from app.db import Database
from app.models import TrackingSample
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.samples import SampleStore


def TrackingStore(max_samples: int = MAX_HISTORY_SAMPLES, db: Database | None = None) -> SampleStore[TrackingSample]:
    return SampleStore("tracking_samples", TrackingSample, max_samples, db)


def LatestTrackingStore(db: Database | None = None) -> SampleStore[TrackingSample]:
    return TrackingStore(db=db)


def TrackingHistoryStore(max_samples: int | None = None, db: Database | None = None) -> SampleStore[TrackingSample]:
    return TrackingStore(MAX_HISTORY_SAMPLES if max_samples is None else max_samples, db)


tracking_store = TrackingStore()
latest_tracking_store = tracking_store
tracking_history_store = tracking_store
