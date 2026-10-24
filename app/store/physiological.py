"""Latest PhysiologicalSample per athlete, plus bounded history (in-memory)."""

from app.models import PhysiologicalSample
from app.store.history import SampleHistoryStore
from app.store.latest import LatestSampleStore


def LatestPhysiologicalStore() -> LatestSampleStore[PhysiologicalSample]:
    return LatestSampleStore(key=lambda s: s.athlete_id)


def PhysiologicalHistoryStore(max_samples: int | None = None) -> SampleHistoryStore[PhysiologicalSample]:
    return SampleHistoryStore() if max_samples is None else SampleHistoryStore(max_samples)


latest_physiological_store = LatestPhysiologicalStore()
physiological_history_store = PhysiologicalHistoryStore()
