"""PhysiologicalSample storage: one SQLite-backed store serves both latest-sample and history reads."""

from app.db import Database
from app.models import PhysiologicalSample
from app.store.samples import SampleStore


def PhysiologicalStore(max_samples: int | None = None, db: Database | None = None) -> SampleStore[PhysiologicalSample]:
    """A physiological store; `max_samples` pins a fixed cap, otherwise the configured PHYSIOLOGICAL_RETENTION_SAMPLES applies at each write."""
    return SampleStore("physiological_samples", PhysiologicalSample, max_samples, db)


def LatestPhysiologicalStore(db: Database | None = None) -> SampleStore[PhysiologicalSample]:
    return PhysiologicalStore(db=db)


def PhysiologicalHistoryStore(max_samples: int | None = None, db: Database | None = None) -> SampleStore[PhysiologicalSample]:
    return PhysiologicalStore(max_samples, db)


physiological_store = PhysiologicalStore()
# Both names refer to the same rows; kept for callers that think in terms of "latest" vs "history".
latest_physiological_store = physiological_store
physiological_history_store = physiological_store
