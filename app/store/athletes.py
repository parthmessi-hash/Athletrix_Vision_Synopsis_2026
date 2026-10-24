"""In-memory athlete registry keyed by Athlete.id.

Not persistent and not shared across processes. This is a placeholder until a
real database is introduced. Registration is not required for sample
ingestion yet, so unregistered athlete_ids are still accepted by the sample
endpoints.
"""

from app.models import Athlete


class AthleteRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, Athlete] = {}

    def add(self, athlete: Athlete) -> bool:
        """Register the athlete. Returns False, without overwriting, if the id already exists."""
        if athlete.id in self._by_id:
            return False
        self._by_id[athlete.id] = athlete
        return True

    def get(self, athlete_id: str) -> Athlete | None:
        return self._by_id.get(athlete_id)

    def list(self) -> list[Athlete]:
        """All registered athletes sorted by id."""
        return [self._by_id[k] for k in sorted(self._by_id)]

    def clear(self) -> None:
        self._by_id.clear()


athlete_registry = AthleteRegistry()
