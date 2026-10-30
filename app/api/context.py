"""Coach-context entries of a recording: add, list, and the entries in force at an instant (app/models/context.py).

Context is entered by people (a coach, an assistant, an analyst) and stored with the recording, so it is reviewed,
exported in packages and removed together with the recording's other data. The engine validates structure, scope and
time bounds and never interprets, scores or combines the notes.
"""

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import AwareDatetime

from app.api.recordings import require_member, require_recording
from app.core import clock
from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.db import get_database
from app.models.context import ContextCreate, ContextEntry, EffectiveContext
from app.store.context import context_store

router = APIRouter(prefix="/recordings", tags=["coach context"])


@router.post("/{recording_id}/context", response_model=ContextEntry, status_code=status.HTTP_201_CREATED,
             responses={404: {"description": "Unknown recording, or an athlete or partner not in its roster"}, 422: {"description": "Invalid entry or effective time outside the recording"}})
def add_context(recording_id: str, body: ContextCreate) -> ContextEntry:
    """Store one context entry for an active or stopped recording.

    `effective_from` must lie within the recording: at or after its start and at or before its stop (or, while it is
    active, no later than backend time plus the usual 5 s skew). Entries are never edited: to correct one, add another
    with the same `effective_from`; the later-created entry is then the one in force.
    """
    now = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        if body.athlete_id is not None:
            require_member(rec, body.athlete_id)
        for p in body.partnerships.items:
            require_member(rec, p.partner_athlete_id)
    latest = rec.stopped_at if rec.stopped_at is not None else now + MAX_FUTURE_TIMESTAMP_SKEW
    if not (rec.started_at <= body.effective_from <= latest):
        raise HTTPException(status_code=422, detail=f"effective_from must lie within recording '{rec.id}' ({rec.started_at.isoformat()} to {latest.isoformat()})")
    for item in body.evidence.items:
        if item.observed_at is not None and not (rec.started_at <= item.observed_at <= latest):
            raise HTTPException(status_code=422, detail="evidence observed_at must lie within the recording")
    return context_store.add(rec.id, body, now)


@router.get("/{recording_id}/context", response_model=list[ContextEntry])
def list_context(recording_id: str, athlete_id: str | None = Query(None, description="Only this athlete's entries"),
                 team: bool = Query(False, description="Only team-level entries")) -> list[ContextEntry]:
    """Every context entry of the recording (or one athlete's, or the team's), by effective_from then creation time."""
    with get_database().read():
        rec = require_recording(recording_id)
        if athlete_id is not None:
            require_member(rec, athlete_id)
        return context_store.list(rec.id, athlete_id, team_only=team)


@router.get("/{recording_id}/context/effective", response_model=EffectiveContext)
def effective_context(recording_id: str, at: AwareDatetime = Query(..., description="Instant, ISO 8601 with timezone (encode '+' as %2B)"),
                      athlete_id: str | None = Query(None)) -> EffectiveContext:
    """The team-level entry and (with athlete_id) the athlete's entry in force at `at`; null when none applies."""
    with get_database().read():
        rec = require_recording(recording_id)
        if athlete_id is not None:
            require_member(rec, athlete_id)
        team, athlete = context_store.effective(rec.id, at, athlete_id)
    return EffectiveContext(recording_id=rec.id, at=at, athlete_id=athlete_id, team=team, athlete=athlete)
