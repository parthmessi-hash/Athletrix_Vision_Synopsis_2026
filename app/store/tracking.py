"""Latest TrackingSample per athlete (in-memory)."""

from app.models import TrackingSample
from app.store.latest import LatestSampleStore


def LatestTrackingStore() -> LatestSampleStore[TrackingSample]:
    return LatestSampleStore(key=lambda s: s.athlete_id)


latest_tracking_store = LatestTrackingStore()
