"""Latest TrackingSample per athlete, plus bounded history (in-memory)."""

from app.models import TrackingSample
from app.store.history import SampleHistoryStore
from app.store.latest import LatestSampleStore


def LatestTrackingStore() -> LatestSampleStore[TrackingSample]:
    return LatestSampleStore(key=lambda s: s.athlete_id)


def TrackingHistoryStore(max_samples: int | None = None) -> SampleHistoryStore[TrackingSample]:
    return SampleHistoryStore() if max_samples is None else SampleHistoryStore(max_samples)


latest_tracking_store = LatestTrackingStore()
tracking_history_store = TrackingHistoryStore()
