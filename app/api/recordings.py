"""Explicit local recordings: start, list, inspect and stop. Session queries and the export live in the same router.

A recording preserves the samples accepted for its selected athletes while it is active, outside the rolling caps.
It is local SQLite storage on this machine; nothing is uploaded, nothing is deleted automatically, and an active
recording survives a backend restart until it is stopped explicitly.
"""

from fastapi import APIRouter, HTTPException, status

from app.core import clock
from app.db import get_database
from app.models.recording import ActiveRecording, RecordedAthlete, RecordedStream, Recording, RecordingDetail, RecordingStart
from app.store.athletes import athlete_registry
from app.store.recordings import RecordingConflict, recording_store

router = APIRouter(prefix="/recordings", tags=["recordings"])


@router.post("", response_model=Recording, status_code=status.HTTP_201_CREATED, responses={409: {"description": "Another recording is active"}, 422: {"description": "Unknown athlete ids or an invalid name"}})
def start_recording(body: RecordingStart) -> Recording:
    """Start a recording for the named registered athletes; records samples accepted from now on, never existing history.

    The whole selection is validated first: any unregistered id rejects the request and nothing is created. Only one
    recording can be active at a time (409 otherwise). The roster details of the selected athletes are snapshotted.
    """
    with get_database().read():
        athletes = [athlete_registry.get(a) for a in body.athlete_ids]
    unknown = [a for a, found in zip(body.athlete_ids, athletes) if found is None]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unregistered athlete ids: {', '.join(unknown)}; register them first (nothing was created)")
    try:
        return recording_store.start(body.name, athletes, clock.utc_now())
    except RecordingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("", response_model=list[Recording])
def list_recordings() -> list[Recording]:
    """All recordings on this backend, newest start first (active and stopped)."""
    return recording_store.list()


@router.get("/active", response_model=ActiveRecording)
def get_active_recording() -> ActiveRecording:
    """The active recording, or null. An active recording persists across backend restarts until stopped."""
    return ActiveRecording(recording=recording_store.active())


def require_recording(recording_id: str) -> Recording:
    rec = recording_store.get(recording_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recording '{recording_id}' does not exist")
    return rec


@router.get("/{recording_id}", response_model=RecordingDetail)
def get_recording(recording_id: str) -> RecordingDetail:
    """The recording with its roster snapshot and, per athlete and stream, the count and recorded-timestamp bounds of its samples."""
    with get_database().read():
        rec = require_recording(recording_id)
        recorded = []
        for a in rec.athletes:
            p, t = recording_store.retention(rec.id, a.id, "physiological"), recording_store.retention(rec.id, a.id, "tracking")
            recorded.append(RecordedAthlete(athlete=a, physiological=RecordedStream(sample_count=p.count, earliest_timestamp=p.earliest, latest_timestamp=p.latest),
                                            tracking=RecordedStream(sample_count=t.count, earliest_timestamp=t.earliest, latest_timestamp=t.latest)))
    return RecordingDetail(**rec.model_dump(), recorded=recorded)


@router.post("/{recording_id}/stop", response_model=Recording)
def stop_recording(recording_id: str) -> Recording:
    """Stop the recording at backend time now. Stopping an already stopped recording returns it unchanged (its original stop time)."""
    rec = recording_store.stop(recording_id, clock.utc_now())
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recording '{recording_id}' does not exist")
    return rec
