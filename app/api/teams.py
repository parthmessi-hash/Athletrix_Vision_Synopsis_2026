"""Team-level live overview: one snapshot of every registered athlete on a team."""

from fastapi import APIRouter

from app.core import clock
from app.db import get_database
from app.models import ReportedDevice, TeamAthleteLive, TeamLive
from app.store.athletes import athlete_registry
from app.store.device import latest_device_status_store
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
    with get_database().read():  # one consistent snapshot of the registry, both sample tables and the device-status table
        members = athlete_registry.list_team(team_id)
        latest = [(a, latest_physiological_store.latest_at_or_before(a.id, captured_at), latest_tracking_store.latest_at_or_before(a.id, captured_at)) for a in members]
        wanted = [s.device_id for _, p, t in latest for s in (p, t) if s is not None and s.device_id]
        statuses = latest_device_status_store.get_many(wanted)  # one query for every reported device id
    rows = []
    for athlete, phys, track in latest:
        rows.append(TeamAthleteLive(
            athlete=athlete,
            physiological=phys,
            tracking=track,
            physiological_age_seconds=(captured_at - phys.timestamp).total_seconds() if phys else None,
            tracking_age_seconds=(captured_at - track.timestamp).total_seconds() if track else None,
            devices=reported_devices(phys, track, statuses, captured_at),
        ))
    return TeamLive(team_id=team_id, captured_at=captured_at, athletes=rows)


def reported_devices(phys, track, statuses: dict, captured_at) -> list[ReportedDevice]:
    """The device ids named by the two newest samples, deduplicated (a shared id lists both streams), each with its status if any.

    Only ids explicitly reported in those samples count; nothing is inferred or assigned. Order: physiological first.
    """
    streams: dict[str, list[str]] = {}
    for name, sample in (("physiological", phys), ("tracking", track)):
        if sample is not None and sample.device_id:
            streams.setdefault(sample.device_id, []).append(name)
    out = []
    for device_id, names in streams.items():
        status = statuses.get(device_id)
        if status is None:
            out.append(ReportedDevice(device_id=device_id, streams=names))
            continue
        delta = (captured_at - status.timestamp).total_seconds()
        # Clock skew, made explicit: a report timestamped after captured_at was accepted within the ingestion skew
        # allowance. It is still the newest report held, so it is returned with age 0 and the amount it is ahead;
        # an age is never negative and the report is never presented as more current than captured_at.
        out.append(ReportedDevice(device_id=device_id, streams=names, status=status, status_age_seconds=max(0.0, delta), status_ahead_seconds=max(0.0, -delta)))
    return out
