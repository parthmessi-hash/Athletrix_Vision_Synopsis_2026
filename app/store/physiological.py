"""Latest PhysiologicalSample per athlete (in-memory)."""

from app.models import PhysiologicalSample
from app.store.latest import LatestSampleStore


def LatestPhysiologicalStore() -> LatestSampleStore[PhysiologicalSample]:
    return LatestSampleStore(key=lambda s: s.athlete_id)


latest_physiological_store = LatestPhysiologicalStore()
