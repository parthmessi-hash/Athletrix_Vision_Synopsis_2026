"""Generic in-memory store of the latest timestamped record per key.

Not persistent and not shared across processes. This is a placeholder until a
real database is introduced.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Generic, Protocol, TypeVar


class _Timestamped(Protocol):
    timestamp: datetime


S = TypeVar("S", bound=_Timestamped)


class LatestSampleStore(Generic[S]):
    def __init__(self, key: Callable[[S], str]) -> None:
        """`key` extracts the identifier records are grouped by (e.g. athlete_id, device_id)."""
        self._key = key
        self._latest: dict[str, S] = {}

    def upsert(self, sample: S) -> S:
        """Keep the record with the newest timestamp for its key.

        On an equal timestamp the most recently received record wins.
        Returns whichever record is now stored for that key.
        """
        k = self._key(sample)
        current = self._latest.get(k)
        if current is None or sample.timestamp >= current.timestamp:
            self._latest[k] = sample
            return sample
        return current

    def get(self, key: str) -> S | None:
        return self._latest.get(key)

    def clear(self) -> None:
        self._latest.clear()
