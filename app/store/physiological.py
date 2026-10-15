"""In-memory store of the latest PhysiologicalSample per athlete.

Not persistent and not shared across processes. This is a placeholder until a
real database is introduced.
"""

from app.models import PhysiologicalSample


class LatestPhysiologicalStore:
    def __init__(self) -> None:
        self._latest: dict[str, PhysiologicalSample] = {}

    def upsert(self, sample: PhysiologicalSample) -> PhysiologicalSample:
        """Keep the sample with the newest timestamp for the athlete.

        Returns whichever sample is now stored for that athlete.
        """
        current = self._latest.get(sample.athlete_id)
        if current is None or sample.timestamp >= current.timestamp:
            self._latest[sample.athlete_id] = sample
            return sample
        return current

    def get(self, athlete_id: str) -> PhysiologicalSample | None:
        return self._latest.get(athlete_id)

    def clear(self) -> None:
        self._latest.clear()


latest_physiological_store = LatestPhysiologicalStore()
