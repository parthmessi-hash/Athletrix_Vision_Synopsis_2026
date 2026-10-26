"""Team-level live overview: one snapshot of every registered athlete on a team."""

from fastapi import APIRouter

from app.core import clock
from app.core.live import select_live
from app.db import get_database
from app.models import TeamAthleteLive, TeamLive
from app.store.athletes import athlete_registry

router = APIRouter(prefix="/teams", tags=["teams"])


@router.get("/{team_id}/live", response_model=TeamLive)
def get_team_live(team_id: str) -> TeamLive:
    """Every athlete registered with this team id, with their newest retained sample per stream at or before one captured instant.

    Membership comes from Athlete.team_id in the registry (no separate team registry: an unknown team has no athletes).
    Backend time is read once; samples timestamped after it, even if accepted within the clock-skew allowance, are not
    shown, so ages are never negative. Old samples are still returned with their age. All reads happen under one lock,
    through the same selection the single-athlete live endpoint uses.
    """
    captured_at = clock.utc_now()
    with get_database().read():  # one consistent snapshot of the registry, both sample tables and the device-status table
        members = athlete_registry.list_team(team_id)
        selection = select_live([a.id for a in members], captured_at)
    rows = [
        TeamAthleteLive(
            athlete=athlete,
            physiological=sel.physiological,
            tracking=sel.tracking,
            physiological_age_seconds=sel.physiological_age_seconds,
            tracking_age_seconds=sel.tracking_age_seconds,
            physiological_newest_ahead_seconds=sel.physiological_newest_ahead_seconds,
            tracking_newest_ahead_seconds=sel.tracking_newest_ahead_seconds,
            devices=sel.devices,
        )
        for athlete, sel in ((a, selection[a.id]) for a in members)
    ]
    return TeamLive(team_id=team_id, captured_at=captured_at, athletes=rows)
