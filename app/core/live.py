"""Shared live-snapshot selection for the team overview and the single-athlete live endpoint.

One captured instant; per athlete the newest retained sample per stream at or before it, the ages, how far ahead of
the instant the newest stored sample is when it has not become current yet, and the device statuses associated
through the device ids those samples reported. Both endpoints call `select_live` under one database read, so a team
row and the athlete's own snapshot agree whenever they share a captured instant.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.models import ReportedDevice
from app.store.device import latest_device_status_store
from app.store.physiological import latest_physiological_store
from app.store.tracking import latest_tracking_store


@dataclass
class LiveSelection:
    physiological: object | None = None
    tracking: object | None = None
    physiological_age_seconds: float | None = None
    tracking_age_seconds: float | None = None
    physiological_newest_ahead_seconds: float | None = None
    tracking_newest_ahead_seconds: float | None = None
    devices: list = field(default_factory=list)


def reported_devices(phys, track, statuses: dict, captured_at: datetime) -> list[ReportedDevice]:
    """The device ids named by the two newest samples, deduplicated (a shared id lists both streams), each with its status if any.

    Only ids explicitly reported in those samples count; nothing is inferred or assigned. Order: physiological first.
    Clock skew is explicit: a status timestamped after captured_at (accepted within the ingestion skew allowance) is still
    the newest report held, so it is returned with age 0 and `status_ahead_seconds`; an age is never negative and the report
    is never presented as more current than captured_at.
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
        out.append(ReportedDevice(device_id=device_id, streams=names, status=status, status_age_seconds=max(0.0, delta), status_ahead_seconds=max(0.0, -delta)))
    return out


def _ahead(newest, chosen, captured_at: datetime) -> float | None:
    """Seconds by which the newest stored sample lies after captured_at when it is not the chosen one; None otherwise."""
    if newest is None or (chosen is not None and newest.timestamp == chosen.timestamp):
        return None
    ahead = (newest.timestamp - captured_at).total_seconds()
    return ahead if ahead > 0 else None


def select_live(athlete_ids: list[str], captured_at: datetime) -> dict[str, LiveSelection]:
    """Per athlete id: the live selection at `captured_at`. Call inside one `get_database().read()` for a consistent snapshot.

    Samples timestamped after captured_at (accepted within the ingestion clock-skew allowance) are not chosen; when
    the newest stored sample is such a sample, `*_newest_ahead_seconds` says how far ahead it is so a consumer can say
    "a newer sample exists but is not current yet" instead of showing it as fresh. The plain `latest` endpoints keep
    returning the newest stored sample regardless.
    """
    result: dict[str, LiveSelection] = {}
    for athlete_id in athlete_ids:
        phys = latest_physiological_store.latest_at_or_before(athlete_id, captured_at)
        track = latest_tracking_store.latest_at_or_before(athlete_id, captured_at)
        sel = LiveSelection(
            physiological=phys,
            tracking=track,
            physiological_age_seconds=(captured_at - phys.timestamp).total_seconds() if phys else None,
            tracking_age_seconds=(captured_at - track.timestamp).total_seconds() if track else None,
            physiological_newest_ahead_seconds=_ahead(latest_physiological_store.get(athlete_id), phys, captured_at),
            tracking_newest_ahead_seconds=_ahead(latest_tracking_store.get(athlete_id), track, captured_at),
        )
        result[athlete_id] = sel
    wanted = [s.device_id for sel in result.values() for s in (sel.physiological, sel.tracking) if s is not None and s.device_id]
    statuses = latest_device_status_store.get_many(wanted)  # one query for every reported device id
    for sel in result.values():
        sel.devices = reported_devices(sel.physiological, sel.tracking, statuses, captured_at)
    return result
