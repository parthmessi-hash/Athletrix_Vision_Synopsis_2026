"""In-memory, bounded, timestamp-ordered sample history per athlete.

Temporary memory only: not persistent, not shared across processes, and lost
on restart. Each athlete keeps at most MAX_HISTORY_SAMPLES of their newest
samples by timestamp. That is a sample count, not a time window: at one
sample per second it is five minutes, at ten per second it is thirty seconds.
"""

from bisect import bisect_left
from datetime import datetime
from typing import Generic, Protocol, TypeVar

# Newest samples kept per athlete. A count, not a duration (see module docstring).
MAX_HISTORY_SAMPLES = 300


class _AthleteSample(Protocol):
    athlete_id: str
    timestamp: datetime


S = TypeVar("S", bound=_AthleteSample)


class SampleHistoryStore(Generic[S]):
    def __init__(self, max_samples: int = MAX_HISTORY_SAMPLES) -> None:
        self._max = max_samples
        self._by_athlete: dict[str, list[S]] = {}

    def add(self, sample: S) -> None:
        """Insert in timestamp order, replacing an equal-timestamp entry, then drop the oldest beyond the cap."""
        entries = self._by_athlete.setdefault(sample.athlete_id, [])
        i = bisect_left(entries, sample.timestamp, key=lambda s: s.timestamp)
        if i < len(entries) and entries[i].timestamp == sample.timestamp:
            entries[i] = sample
        else:
            entries.insert(i, sample)
        if len(entries) > self._max:
            del entries[: len(entries) - self._max]

    def recent(self, athlete_id: str, limit: int) -> list[S]:
        """The newest `limit` samples for the athlete, oldest to newest. Empty if none."""
        entries = self._by_athlete.get(athlete_id, [])
        return entries[-limit:] if limit < len(entries) else list(entries)

    def count(self, athlete_id: str) -> int:
        return len(self._by_athlete.get(athlete_id, []))

    def clear(self) -> None:
        self._by_athlete.clear()
