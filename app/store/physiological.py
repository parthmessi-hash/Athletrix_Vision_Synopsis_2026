"""PhysiologicalSample storage: one SQLite-backed store serves both latest-sample and history reads."""

from app.db import Database
from app.models import PhysiologicalSample
from app.store.history import MAX_HISTORY_SAMPLES
from app.store.samples import SampleStore


def PhysiologicalStore(max_samples: int = MAX_HISTORY_SAMPLES, db: Database | None = None) -> SampleStore[PhysiologicalSample]:
    return SampleStore("physiological_samples", PhysiologicalSample, max_samples, db)


def LatestPhysiologicalStore(db: Database | None = None) -> SampleStore[PhysiologicalSample]:
    return PhysiologicalStore(db=db)


def PhysiologicalHistoryStore(max_samples: int | None = None, db: Database | None = None) -> SampleStore[PhysiologicalSample]:
    return PhysiologicalStore(MAX_HISTORY_SAMPLES if max_samples is None else max_samples, db)


physiological_store = PhysiologicalStore()
# Both names refer to the same rows; kept for callers that think in terms of "latest" vs "history".
latest_physiological_store = physiological_store
physiological_history_store = physiological_store
