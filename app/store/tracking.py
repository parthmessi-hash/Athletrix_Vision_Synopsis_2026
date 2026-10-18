"""Latest TrackingSample per athlete (in-memory)."""

from app.models import TrackingSample
from app.store.latest import LatestSampleStore

LatestTrackingStore = LatestSampleStore[TrackingSample]

latest_tracking_store: LatestTrackingStore = LatestTrackingStore()
