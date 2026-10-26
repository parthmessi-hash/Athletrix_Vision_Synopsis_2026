"""Explicit local recordings: start, list, inspect and stop. Session queries and the export live in the same router.

A recording preserves the samples accepted for its selected athletes while it is active, outside the rolling caps.
It is local SQLite storage on this machine; nothing is uploaded, nothing is deleted automatically, and an active
recording survives a backend restart until it is stopped explicitly.
"""

import json
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import AwareDatetime

from app.api.athletes import JSONL_MEDIA_TYPE, WINDOW_END, WINDOW_SECONDS, resolve_window_end
from app.core import clock
from app.core.availability import session_stream_availability
from app.core.config import get_settings
from app.core.effort import AFTER_SECONDS, BEFORE_SECONDS, EffortRequestError, parse_effort_timestamp, period_bounds, review_effort, select_period, validate_effort
from app.core.export import export_records, to_jsonl
from app.core.summary import summarize_window
from app.db import get_database
from app.models import Athlete, TrackingRules
from app.models.recording import (
    ActiveRecording, RecordedAthlete, RecordedData, RecordedStream, Recording, RecordingDetail, RecordingEffortReview, RecordingRef, RecordingStart, RecordingSummary, RecordingWindow,
    SessionEffortAvailability, SessionHeartRateAvailability, SessionWindowAvailability,
)
from app.models.period import PeriodCreate, SavedPeriod
from app.store.athletes import athlete_registry
from app.store.periods import period_store
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


# ---- session review: queries over one recording's samples only -------------------------------------------------------

def recording_ref(rec: Recording) -> RecordingRef:
    return RecordingRef(id=rec.id, name=rec.name, status=rec.status, started_at=rec.started_at, stopped_at=rec.stopped_at)


def require_member(rec: Recording, athlete_id: str) -> Athlete:
    for a in rec.athletes:
        if a.id == athlete_id:
            return a
    raise HTTPException(status_code=404, detail=f"Athlete '{athlete_id}' is not part of recording '{rec.id}'")


def recorded_stream(rec_id: str, athlete_id: str, stream: str) -> RecordedStream:
    r = recording_store.retention(rec_id, athlete_id, stream)
    return RecordedStream(sample_count=r.count, earliest_timestamp=r.earliest, latest_timestamp=r.latest)


@router.get("/{recording_id}/athletes/{athlete_id}/recorded-data", response_model=RecordedData)
def get_recorded_data(recording_id: str, athlete_id: str) -> RecordedData:
    """Per stream: how many samples the recording holds for the athlete and the UTC recorded timestamps of the oldest and newest. Bounds, not coverage."""
    with get_database().read():
        rec = require_recording(recording_id)
        athlete = require_member(rec, athlete_id)
        return RecordedData(recording=recording_ref(rec), athlete=athlete, physiological=recorded_stream(rec.id, athlete_id, "physiological"), tracking=recorded_stream(rec.id, athlete_id, "tracking"))


def select_recording_window(recording_id: str, athlete_id: str, seconds: int, end: datetime | None) -> RecordingWindow:
    """Both streams of the recording in the `seconds` before one window end, with session availability, from one read.

    Same selection rules as the rolling window (inclusive bounds, oldest to newest, backend time read once, a future
    `end` rejected) but only this recording's rows: no fallback to the rolling history or another recording, and no
    retention cap. The window bounds are the requested analysis period; the recording's own start and stop times are
    reported separately in `recording`.
    """
    window_end = resolve_window_end(end)
    window_start = window_end - timedelta(seconds=seconds)
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        physiological = recording_store.between(rec.id, athlete_id, "physiological", window_start, window_end)
        tracking = recording_store.between(rec.id, athlete_id, "tracking", window_start, window_end)
        p_ret, t_ret = recording_store.retention(rec.id, athlete_id, "physiological"), recording_store.retention(rec.id, athlete_id, "tracking")
    return RecordingWindow(
        athlete_id=athlete_id, window_start=window_start, window_end=window_end,
        physiological_samples=physiological, tracking_samples=tracking,
        tracking_rules=TrackingRules(min_confidence=get_settings().distance_min_confidence),
        recording=recording_ref(rec),
        session_availability=SessionWindowAvailability(physiological=session_stream_availability(p_ret, physiological), tracking=session_stream_availability(t_ret, tracking)),
    )


@router.get("/{recording_id}/athletes/{athlete_id}/window", response_model=RecordingWindow)
def get_recording_window(recording_id: str, athlete_id: str, seconds: int = WINDOW_SECONDS, end: AwareDatetime | None = WINDOW_END) -> RecordingWindow:
    """The recording's physiological and tracking samples for the athlete in the `seconds` before `end` (default: now), oldest to newest, plus session availability."""
    return select_recording_window(recording_id, athlete_id, seconds, end)


@router.get("/{recording_id}/athletes/{athlete_id}/summary", response_model=RecordingSummary)
def get_recording_summary(recording_id: str, athlete_id: str, seconds: int = WINDOW_SECONDS, end: AwareDatetime | None = WINDOW_END) -> RecordingSummary:
    """The same descriptive summary as the rolling summary endpoint (same rules and calculations) over the recording's samples only."""
    settings = get_settings()
    window = select_recording_window(recording_id, athlete_id, seconds, end)
    summary = summarize_window(window, distance_max_gap_seconds=settings.distance_max_gap_seconds, distance_min_confidence=settings.distance_min_confidence, high_speed_threshold_mps=settings.high_speed_threshold_mps)
    return RecordingSummary(**summary.model_dump(), recording=window.recording, session_availability=window.session_availability)


EFFORT_START_Q = "Selected effort start: ISO 8601 with an explicit timezone (encode '+' as %2B)."
EFFORT_END_Q = "Selected effort end, same format; 1 to 120 s after the start and not after backend time."


class SessionReview:
    """One session effort review with the samples it was calculated from (the review endpoint and the export share this)."""

    def __init__(self, review: RecordingEffortReview, physiological: list, tracking: list, periods: dict) -> None:
        self.review, self.physiological, self.tracking, self.periods = review, physiological, tracking, periods


def compute_session_review(recording_id: str, athlete_id: str, start: datetime, end: datetime, now: datetime, settings) -> SessionReview:
    """The effort review over one recording's samples only, from one database read, with the given captured time and settings.

    `physiological` holds the samples of the before, during and after span and `tracking` those of the effort, both as
    stored; the review's figures come from exactly these samples under the given settings.
    """
    validate_effort(start, end, now)
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        physiological = recording_store.between(rec.id, athlete_id, "physiological", start - timedelta(seconds=BEFORE_SECONDS), end + timedelta(seconds=AFTER_SECONDS))
        tracking = recording_store.between(rec.id, athlete_id, "tracking", start, end)
        p_ret, t_ret = recording_store.retention(rec.id, athlete_id, "physiological"), recording_store.retention(rec.id, athlete_id, "tracking")
    review = review_effort(athlete_id, start, end, now, physiological, tracking, distance_max_gap_seconds=settings.distance_max_gap_seconds,
                           distance_min_confidence=settings.distance_min_confidence, high_speed_threshold_mps=settings.high_speed_threshold_mps)
    before, during, after = period_bounds(start, end, now)
    availability = SessionEffortAvailability(
        movement=session_stream_availability(t_ret, [s for s in tracking if start <= s.timestamp <= end]),
        heart_rate=SessionHeartRateAvailability(**{name: session_stream_availability(p_ret, select_period(physiological, period)) for name, period in (("before", before), ("during", during), ("after", after))}),
    )
    periods = {name: {"start": period.start, "end": period.end, "start_inclusive": period.start_inclusive, "end_inclusive": period.end_inclusive} for name, period in (("before", before), ("during", during), ("after", after))}
    return SessionReview(RecordingEffortReview(**review.model_dump(), recording=recording_ref(rec), session_availability=availability), physiological, tracking, periods)


@router.get("/{recording_id}/athletes/{athlete_id}/effort-review", response_model=RecordingEffortReview)
def get_recording_effort_review(recording_id: str, athlete_id: str, effort_start: str | None = None, effort_end: str | None = None) -> RecordingEffortReview:
    """The same effort review as the rolling endpoint (same boundaries, rules and calculations) over the recording's samples only, with session availability per period."""
    try:
        start = parse_effort_timestamp("effort_start", effort_start)
        end = parse_effort_timestamp("effort_end", effort_end)
        return compute_session_review(recording_id, athlete_id, start, end, clock.utc_now(), get_settings()).review
    except EffortRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def recording_export_filename(recording_id: str, athlete_id: str, captured_at: datetime) -> str:
    safe = lambda text, fallback: re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._") or fallback  # noqa: E731
    return f"athletrix-recording-{safe(recording_id, 'recording')}-{safe(athlete_id, 'athlete')}-{captured_at.strftime('%Y%m%dT%H%M%SZ')}.jsonl"


@router.get(
    "/{recording_id}/athletes/{athlete_id}/recorded-samples.jsonl",
    responses={200: {"content": {JSONL_MEDIA_TYPE: {}}, "description": "JSON Lines: one {type, sample} record per line, oldest first"}, 404: {"description": "Unknown recording or athlete, or nothing recorded for the athlete"}},
)
def export_recording_samples(recording_id: str, athlete_id: str) -> Response:
    """Every sample the recording holds for the athlete, both streams, in the replay-compatible interchange format, written as stored.

    Same format and ordering as the rolling export (physiological before tracking at equal instants). Roster and
    recording metadata are not in the file; they are in the recording detail. No retention cap applies.
    """
    captured_at = clock.utc_now()
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        physiological = recording_store.all(rec.id, athlete_id, "physiological")
        tracking = recording_store.all(rec.id, athlete_id, "tracking")
    if not physiological and not tracking:
        raise HTTPException(status_code=404, detail=f"Recording '{rec.id}' holds no samples for athlete '{athlete_id}' in either stream; nothing to export")
    records = export_records(physiological, tracking)
    return Response(
        content=to_jsonl(records), media_type=JSONL_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{recording_export_filename(rec.id, athlete_id, captured_at)}"',
            "X-Athletrix-Record-Count": str(len(records)), "X-Athletrix-Physiological-Count": str(len(physiological)), "X-Athletrix-Tracking-Count": str(len(tracking)),
            "X-Athletrix-Recording-Id": rec.id,
            "Access-Control-Expose-Headers": "Content-Disposition, X-Athletrix-Record-Count, X-Athletrix-Physiological-Count, X-Athletrix-Tracking-Count, X-Athletrix-Recording-Id",
        },
    )


# ---- saved effort periods (stopped recordings only) --------------------------------------------------------------------

@router.post("/{recording_id}/athletes/{athlete_id}/periods", response_model=SavedPeriod, status_code=status.HTTP_201_CREATED,
             responses={404: {"description": "Unknown recording or athlete not in its roster"}, 409: {"description": "The recording is still active"}, 422: {"description": "Blank label or invalid period"}})
def save_period(recording_id: str, athlete_id: str, body: PeriodCreate) -> SavedPeriod:
    """Save a manually selected effort period for an athlete of a stopped recording: the label and the exact bounds, nothing calculated.

    The bounds follow the effort review's rules (explicit timezone, 1 to 120 s, not after backend time) and are stored
    as sent. Labels are not identifiers and may repeat. Empty or partial phases are a valid outcome when the period is
    reviewed later; nothing here checks for measurements.
    """
    try:
        start = parse_effort_timestamp("effort_start", body.effort_start)
        end = parse_effort_timestamp("effort_end", body.effort_end)
        now = clock.utc_now()
        validate_effort(start, end, now)
    except EffortRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rec = require_recording(recording_id)
    require_member(rec, athlete_id)
    if rec.status != "stopped":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Recording '{rec.id}' is still active; stop it before saving periods so the underlying samples are stable")
    return period_store.add(rec.id, athlete_id, body.label, start, end, now)


@router.get("/{recording_id}/athletes/{athlete_id}/periods", response_model=list[SavedPeriod])
def list_periods(recording_id: str, athlete_id: str) -> list[SavedPeriod]:
    """The athlete's saved periods in this recording, ordered by start instant."""
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        return period_store.list(rec.id, athlete_id)


def require_period(rec: Recording, athlete_id: str, period_id: str) -> SavedPeriod:
    period = period_store.get(rec.id, athlete_id, period_id)
    if period is None:
        raise HTTPException(status_code=404, detail=f"Saved period '{period_id}' does not exist for athlete '{athlete_id}' in recording '{rec.id}'")
    return period


@router.get("/{recording_id}/athletes/{athlete_id}/periods/{period_id}", response_model=SavedPeriod)
def get_period(recording_id: str, athlete_id: str, period_id: str) -> SavedPeriod:
    """One saved period (404 when it belongs to another recording or athlete, or does not exist)."""
    with get_database().read():
        rec = require_recording(recording_id)
        require_member(rec, athlete_id)
        return require_period(rec, athlete_id, period_id)


# ---- descriptive review snapshot of a saved period (analysis export, separate from the sample JSONL) -------------------

PERIOD_REVIEW_FORMAT = "athletrix-period-review"
PERIOD_REVIEW_VERSION = 1


def period_review_filename(recording_id: str, athlete_id: str, period_id: str, captured_at: datetime) -> str:
    safe = lambda text, fallback: re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._") or fallback  # noqa: E731
    return f"athletrix-period-review-{safe(recording_id, 'recording')}-{safe(athlete_id, 'athlete')}-{safe(period_id, 'period')}-{captured_at.strftime('%Y%m%dT%H%M%SZ')}.json"


def json_default(v):
    """Datetimes as the models write them (UTC as 'Z', other offsets kept); anything else as text."""
    if isinstance(v, datetime):
        return v.isoformat().replace("+00:00", "Z")
    return str(v)


def build_period_review(rec: Recording, athlete: Athlete, period: SavedPeriod, computed: SessionReview, settings, captured_at: datetime) -> dict:
    """The exported document: the saved selection, the rules used now, and this snapshot of the result with its samples."""
    review = computed.review
    return {
        "format": PERIOD_REVIEW_FORMAT,
        "format_version": PERIOD_REVIEW_VERSION,
        "generated_at": captured_at,
        "what_this_is": "A descriptive snapshot of one effort review calculated at generated_at from the recording's samples with the engine rules listed under calculation. "
                        "The saved period is the selection only; a later review of the same period with other engine settings gives other figures, and this file keeps the earlier ones. "
                        "No diagnosis, score, baseline comparison or recommendation is part of it.",
        "recording": {**recording_ref(rec).model_dump(mode="json"), "athletes": [a.model_dump(mode="json") for a in rec.athletes]},
        "athlete": athlete.model_dump(mode="json"),
        "saved_period": period.model_dump(mode="json"),
        "calculation": {
            "captured_at": captured_at,
            "rules": review.rules.model_dump(mode="json"),
            "distance_rules": {"max_gap_seconds": settings.distance_max_gap_seconds, "min_confidence": settings.distance_min_confidence, "high_speed_threshold_mps": settings.high_speed_threshold_mps},
            "tracking_rules": {"min_confidence": settings.distance_min_confidence},
            "note": "Rules in force when this snapshot was calculated; they are engine settings, not sport standards, and are not stored with the saved period.",
        },
        "periods": {name: {"start": v["start"], "end": v["end"], "start_inclusive": v["start_inclusive"], "end_inclusive": v["end_inclusive"]} for name, v in computed.periods.items()},
        "review": review.model_dump(mode="json"),
        "samples": {
            "physiological": [s.model_dump(mode="json") for s in computed.physiological],
            "tracking": [s.model_dump(mode="json") for s in computed.tracking],
            "note": "The normalised samples the review was calculated from, as stored: physiological samples across the before, during and after span, tracking samples within the effort. Nulls, zeros, confidences, sources and device ids are kept.",
        },
    }


@router.get(
    "/{recording_id}/athletes/{athlete_id}/periods/{period_id}/review.json",
    responses={200: {"content": {"application/json": {}}, "description": "Descriptive review snapshot of the saved period"}, 404: {"description": "Unknown recording, athlete or period"}},
)
def export_period_review(recording_id: str, athlete_id: str, period_id: str) -> Response:
    """Download a descriptive JSON review of a saved period: the saved selection, the rules used now, and this snapshot of the result with its samples.

    One database read and one captured set of engine settings produce the whole document, which reuses the session
    effort review's selection and calculations. It is an analysis export, separate from the replay-compatible sample
    JSONL export, and it is a snapshot: the saved period itself never freezes a calculation.
    """
    captured_at = clock.utc_now()
    settings = get_settings()
    with get_database().read():
        rec = require_recording(recording_id)
        athlete = require_member(rec, athlete_id)
        period = require_period(rec, athlete_id, period_id)
        try:
            computed = compute_session_review(rec.id, athlete_id, period.effort_start, period.effort_end, captured_at, settings)
        except EffortRequestError as exc:  # cannot happen for a saved period unless the clock went backwards
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    document = build_period_review(rec, athlete, period, computed, settings, captured_at)
    body = json.dumps(document, ensure_ascii=False, indent=2, default=json_default)
    return Response(
        content=body, media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{period_review_filename(rec.id, athlete_id, period.id, captured_at)}"',
            "X-Athletrix-Format": f"{PERIOD_REVIEW_FORMAT}/{PERIOD_REVIEW_VERSION}", "X-Athletrix-Recording-Id": rec.id, "X-Athletrix-Period-Id": period.id,
            "Access-Control-Expose-Headers": "Content-Disposition, X-Athletrix-Format, X-Athletrix-Recording-Id, X-Athletrix-Period-Id",
        },
    )


@router.post("/{recording_id}/stop", response_model=Recording)
def stop_recording(recording_id: str) -> Recording:
    """Stop the recording at backend time now. Stopping an already stopped recording returns it unchanged (its original stop time)."""
    rec = recording_store.stop(recording_id, clock.utc_now())
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Recording '{recording_id}' does not exist")
    return rec
