"""Team-level live overview: one snapshot of every registered athlete on a team."""

from fastapi import APIRouter

from app.core import clock
from app.db import get_database
from app.models import TeamAthleteLive, TeamLive
from app.store.athletes import athlete_registry
from app.store.physiological import latest_physiological_store
from app.store.tracking import latest_tracking_store

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("/{team_id}/live", response_model=TeamLive)
def get_team_live(team_id: str) -> TeamLive:
    """Every athlete registered with this team id, with their newest retained sample per stream at or before one captured instant.

    Membership comes from Athlete.team_id in the registry (no separate team registry: an unknown team has no athletes).
    Backend time is read once; samples timestamped after it, even if accepted within the clock-skew allowance, are not
    shown, so ages are never negative. Old samples are still returned with their age. All reads happen under one lock.
    """
    captured_at = clock.utc_now()
    with get_database().read():  # one consistent snapshot of the registry and both sample tables
        members = athlete_registry.list_team(team_id)
        rows = []
        for athlete in members:
            phys = latest_physiological_store.latest_at_or_before(athlete.id, captured_at)
            track = latest_tracking_store.latest_at_or_before(athlete.id, captured_at)
            rows.append(TeamAthleteLive(
                athlete=athlete,
                physiological=phys,
                tracking=track,
                physiological_age_seconds=(captured_at - phys.timestamp).total_seconds() if phys else None,
                tracking_age_seconds=(captured_at - track.timestamp).total_seconds() if track else None,
            ))
    return TeamLive(team_id=team_id, captured_at=captured_at, athletes=rows)
