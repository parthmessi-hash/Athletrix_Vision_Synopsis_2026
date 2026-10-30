"""Individual-performance evidence of a recording: add, list, delete, and the per-athlete review (evidence and, only
with a configured rubric and enough evidence, area scores and a composite). See app/models/performance.py."""

from fastapi import APIRouter, HTTPException, status

from app.api.recordings import require_member, require_recording
from app.core import clock
from app.core.clock import MAX_FUTURE_TIMESTAMP_SKEW
from app.core.config import get_settings
from app.core.performance import RubricError, load_rubric, review
from app.db import get_database
from app.models.performance import PerformanceObservation, PerformanceObservationCreate
from app.store.context import context_store
from app.store.performance import performance_store

router = APIRouter(prefix="/recordings", tags=["performance evidence"])


@router.post("/{recording_id}/performance", response_model=PerformanceObservation, status_code=status.HTTP_201_CREATED,
             responses={404: {"description": "Unknown recording, athlete not in its roster, or unknown coach-context entry"}, 422: {"description": "Invalid observation"}})
def add_observation(recording_id: str, body: PerformanceObservationCreate) -> PerformanceObservation:
    """Store one evidence observation (manual entry). The observation period must lie within the recording. Automatic
    camera recognition does not exist in this version, so evidence_source "camera_automatic" is refused."""
    if body.evidence_source == "camera_automatic":
        raise HTTPException(status_code=422, detail="this version has no automatic camera event recognition; record the observation as human_annotation or human_review_of_camera")
    now = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, body.athlete_id)
        if body.coach_context_id is not None:
            ids = {e.id: e for e in context_store.list(rec.id)}
            entry = ids.get(body.coach_context_id)
            if entry is None:
                raise HTTPException(status_code=404, detail=f"coach-context entry {body.coach_context_id!r} does not belong to recording {rec.id!r}")
            if entry.athlete_id not in (None, body.athlete_id):
                raise HTTPException(status_code=422, detail="the coach-context entry belongs to another athlete")
    latest = rec.stopped_at if rec.stopped_at is not None else now + MAX_FUTURE_TIMESTAMP_SKEW
    if not (rec.started_at <= body.period_start and body.period_end <= latest):
        raise HTTPException(status_code=422, detail=f"the observation period must lie within recording '{rec.id}'")
    return performance_store.add(rec.id, body, now)


@router.get("/{recording_id}/performance", response_model=list[PerformanceObservation])
def list_observations(recording_id: str) -> list[PerformanceObservation]:
    with get_database().read():
        rec = require_recording(recording_id)
        return performance_store.list(rec.id)


@router.delete("/{recording_id}/athletes/{athlete_id}/performance/{observation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_observation(recording_id: str, athlete_id: str, observation_id: str) -> None:
    """Delete one observation (for example one entered by mistake). Not reversible."""
    rec = require_recording(recording_id)
    require_member(rec, athlete_id)
    if not performance_store.delete(rec.id, athlete_id, observation_id):
        raise HTTPException(status_code=404, detail=f"observation {observation_id!r} does not exist for this athlete and recording")


@router.get("/{recording_id}/athletes/{athlete_id}/performance-review")
def performance_review(recording_id: str, athlete_id: str) -> dict:
    """Evidence per area and, only with a configured rubric and enough assessed evidence in all four areas, a composite."""
    try:
        rubric, sha = load_rubric(get_settings().performance_rubric_path)
    except (RubricError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    with get_database().read():
        rec = require_recording(recording_id)
        athlete = require_member(rec, athlete_id)
        obs = performance_store.list(rec.id, athlete_id)
    return {"recording_id": rec.id, "athlete": athlete.model_dump(mode="json"), **review(obs, rubric, sha)}
