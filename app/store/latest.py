"""Generic in-memory store of the latest sample per athlete.

Not persistent and not shared across processes. This is a placeholder until a
real database is introduced.
"""

from datetime import datetime
from typing import Generic, Protocol, TypeVar


class _AthleteSample(Protocol):
    athlete_id: str
    timestamp: datetime


S = TypeVar("S", bound=_AthleteSample)


class LatestSampleStore(Generic[S]):
    def __init__(self) -> None:
        self._latest: dict[str, S] = {}

    def upsert(self, sample: S) -> S:
        """Keep the sample with the newest timestamp for the athlete.

        On an equal timestamp the most recently received sample wins.
        Returns whichever sample is now stored for that athlete.
        """
        current = self._latest.get(sample.athlete_id)
        if current is None or sample.timestamp >= current.timestamp:
            self._latest[sample.athlete_id] = sample
            return sample
        return current

    def get(self, athlete_id: str) -> S | None:
        return self._latest.get(athlete_id)

    def clear(self) -> None:
        self._latest.clear()
