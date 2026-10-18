"""Latest PhysiologicalSample per athlete (in-memory)."""

from app.models import PhysiologicalSample
from app.store.latest import LatestSampleStore

LatestPhysiologicalStore = LatestSampleStore[PhysiologicalSample]

latest_physiological_store: LatestPhysiologicalStore = LatestPhysiologicalStore()
