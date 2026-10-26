"""TrackingSample storage: one SQLite-backed store serves both latest-sample and history reads."""

from app.db import Database
from app.models import TrackingSample
from app.store.samples import SampleStore


def TrackingStore(max_samples: int | None = None, db: Database | None = None) -> SampleStore[TrackingSample]:
    """A tracking store; `max_samples` pins a fixed cap, otherwise the configured TRACKING_RETENTION_SAMPLES applies at each write."""
    return SampleStore("tracking_samples", TrackingSample, max_samples, db)


def LatestTrackingStore(db: Database | None = None) -> SampleStore[TrackingSample]:
    return TrackingStore(db=db)


def TrackingHistoryStore(max_samples: int | None = None, db: Database | None = None) -> SampleStore[TrackingSample]:
    return TrackingStore(max_samples, db)


tracking_store = TrackingStore()
# Both names refer to the same rows; kept for callers that think in terms of "latest" vs "history".
latest_tracking_store = tracking_store
tracking_history_store = tracking_store
