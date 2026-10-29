from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime

from app.core import clock
from app.core.availability import stream_availability
from app.core.config import get_settings
from app.core.effort import BEFORE_SECONDS, AFTER_SECONDS, EffortRequestError, parse_effort_timestamp, review_effort
from app.core.export import export_filename, export_records, to_jsonl
from app.core.live import select_live
from app.db import get_database
from app.core.summary import summarize_window
from app.models import Athlete, AthleteLive, AthleteSummary, AthleteWindow, EffortReview, PhysiologicalSample, RetainedData, StreamRetention, TrackingRules, TrackingSample, WindowAvailability
from app.store.athletes import athlete_registry
from app.store.history import retention_caps
from app.store.physiological import latest_physiological_store, physiological_history_store
from app.store.tracking import latest_tracking_store, tracking_history_store

router = APIRouter(prefix="/athletes", tags=["athletes"])

# Longest window that may be requested, in seconds. An API limit on one request, separate from the retention caps.
MAX_WINDOW_SECONDS = 300
# Most samples one history request returns. A page-size limit on one request, separate from the retention caps:
# a stream whose cap is larger is read in full through the retained-samples export, not through history.
MAX_HISTORY_QUERY_LIMIT = 300


# ---- registry --------------------------------------------------------------

@router.post("", response_model=Athlete, status_code=status.HTTP_201_CREATED)
def register_athlete(athlete: Athlete) -> Athlete:
    """Register an athlete. 409 if the id is already registered (existing record is kept)."""
    if not athlete_registry.add(athlete):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Athlete '{athlete.id}' is already registered")
    return athlete


@router.get("", response_model=list[Athlete])
def list_athletes() -> list[Athlete]:
    """All registered athletes sorted by id; empty list if none."""
    return athlete_registry.list()


@router.post("/validate", response_model=Athlete)
def validate_athlete(athlete: Athlete) -> Athlete:
    """Validate an athlete payload and echo it back without registering it."""
    return athlete


@router.get("/{athlete_id}", response_model=Athlete)
def get_athlete(athlete_id: str) -> Athlete:
    """Return a registered athlete, or 404."""
    athlete = athlete_registry.get(athlete_id)
    if athlete is None:
        raise HTTPException(status_code=404, detail=f"Athlete '{athlete_id}' is not registered")
    return athlete


# ---- latest samples (registration not required) ----------------------------

@router.get("/{athlete_id}/physiological/latest", response_model=PhysiologicalSample)
def get_latest_physiological(athlete_id: str) -> PhysiologicalSample:
    """Return the most recent physiological sample received for an athlete."""
    sample = latest_physiological_store.get(athlete_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No physiological sample for athlete '{athlete_id}'")
    return sample


@router.get("/{athlete_id}/physiological/history", response_model=list[PhysiologicalSample])
def get_physiological_history(
    athlete_id: str,
    limit: int = Query(60, ge=1, le=MAX_HISTORY_QUERY_LIMIT, description="Newest samples to return (page size; the export returns every retained sample)"),
) -> list[PhysiologicalSample]:
    """The newest `limit` physiological samples for an athlete, oldest to newest. Empty list if none."""
    return physiological_history_store.recent(athlete_id, limit)


@router.get("/{athlete_id}/tracking/latest", response_model=TrackingSample)
def get_latest_tracking(athlete_id: str) -> TrackingSample:
    """Return the most recent tracking sample received for an athlete."""
    sample = latest_tracking_store.get(athlete_id)
    if sample is None:
        raise HTTPException(status_code=404, detail=f"No tracking sample for athlete '{athlete_id}'")
    return sample


@router.get("/{athlete_id}/tracking/history", response_model=list[TrackingSample])
def get_tracking_history(
    athlete_id: str,
    limit: int = Query(60, ge=1, le=MAX_HISTORY_QUERY_LIMIT, description="Newest samples to return (page size; the export returns every retained sample)"),
) -> list[TrackingSample]:
    """The newest `limit` tracking samples for an athlete, oldest to newest. Empty list if none."""
    return tracking_history_store.recent(athlete_id, limit)


# ---- coherent live snapshot (registration not required) -------------------

@router.get("/{athlete_id}/live", response_model=AthleteLive)
def get_athlete_live(athlete_id: str) -> AthleteLive:
    """The athlete's newest retained sample per stream at or before one captured backend instant, with ages, device associations and clock skew.

    The same selection as the team overview, so the two agree at the same instant. A sample timestamped after the captured
    instant stays stored but is not shown as current; `*_newest_ahead_seconds` says how far ahead it is. The plain
    `latest` endpoints are unchanged and still return the newest stored sample. Registration is not required: an unknown
    id answers with registered=false and whatever it has stored (possibly nothing); it is never a 404.
    """
    captured_at = clock.utc_now()
    with get_database().read():  # one consistent snapshot of the registry, both sample tables and the device-status table
        athlete = athlete_registry.get(athlete_id)
        sel = select_live([athlete_id], captured_at)[athlete_id]
        receipts = {s: (store.receipt_at(athlete_id, sample.timestamp) if sample is not None else None)
                    for s, store, sample in (("physiological", physiological_history_store, sel.physiological), ("tracking", tracking_history_store, sel.tracking))}
    return AthleteLive(
        athlete_id=athlete_id, captured_at=captured_at, registered=athlete is not None, athlete=athlete,
        physiological=sel.physiological, tracking=sel.tracking,
        physiological_age_seconds=sel.physiological_age_seconds, tracking_age_seconds=sel.tracking_age_seconds,
        physiological_newest_ahead_seconds=sel.physiological_newest_ahead_seconds, tracking_newest_ahead_seconds=sel.tracking_newest_ahead_seconds,
        devices=sel.devices, physiological_receipt=receipts["physiological"], tracking_receipt=receipts["tracking"],
    )


# ---- retained data (registration not required) -----------------------------

@router.get("/{athlete_id}/retained-data", response_model=RetainedData)
def get_retained_data(athlete_id: str) -> RetainedData:
    """Per stream: how many samples are currently retained for the athlete, the UTC recorded timestamps of the oldest and newest, and that stream's cap.

    Reads the persisted rows as they are now, both streams under one read. Ranges bound the retained records only: they
    do not show continuous coverage, and a count of 0 (or an unknown athlete id) says nothing about whether data was never
    sent or has been removed by the newest-N retention rule. Streams can cover different times and have their own caps
    (`max_samples`); `max_samples_per_stream` is kept for older consumers and is null when the two caps differ.
    """
    caps = retention_caps()
    with get_database().read():
        phys = physiological_history_store.retention(athlete_id)
        track = tracking_history_store.retention(athlete_id)
    return RetainedData(
        athlete_id=athlete_id,
        physiological=StreamRetention(sample_count=phys.count, earliest_timestamp=phys.earliest, latest_timestamp=phys.latest, max_samples=caps["physiological"]),
        tracking=StreamRetention(sample_count=track.count, earliest_timestamp=track.earliest, latest_timestamp=track.latest, max_samples=caps["tracking"]),
        max_samples_per_stream=caps["physiological"] if caps["physiological"] == caps["tracking"] else None,
    )


WINDOW_SECONDS = Query(60, ge=1, le=MAX_WINDOW_SECONDS, description="Window length ending at `end`, or at backend time now when `end` is omitted")
WINDOW_END = Query(
    None,
    description=(
        "Recorded review: the exact window end as an ISO 8601 timestamp with timezone, e.g. 2026-10-20T10:00:02Z "
        "(encode '+' as %2B). Must not be after backend time. Omitted: live mode, backend time now."
    ),
)


def resolve_window_end(end: datetime | None) -> datetime:
    """The window end for one request: backend time now (live), or the requested recorded instant.

    Backend time is read exactly once either way. A recorded end after backend
    time is rejected, so a review never includes samples that could still
    arrive within the ingestion clock-skew allowance.
    """
    now = clock.utc_now()
    if end is None:
        return now
    if end > now:
        raise HTTPException(
            status_code=422,
            detail=f"end {end.isoformat()} is after backend time {now.isoformat()}; a recorded review cannot end in the future",
        )
    return end


def select_window(athlete_id: str, seconds: int, min_confidence: float, end: datetime | None = None) -> AthleteWindow:
    """Both streams recorded in the last `seconds` before one window end.

    Live (`end` is None): the end is backend time, read once; samples timestamped
    after it are excluded even if ingestion accepted them within the clock-skew
    tolerance. Recorded review (`end` given, timezone-aware, not in the future):
    that exact instant is the end and the start is `seconds` before it. The
    selection, inclusive boundaries, ordering and `tracking_rules` are the same in
    both cases; only the retained samples (the newest rows per athlete up to each
    stream's configured cap) can be reviewed. Reads the stores directly. `min_confidence` is the configured
    DISTANCE_MIN_CONFIDENCE, echoed in `tracking_rules` so the response is
    self-contained.
    """
    window_end = resolve_window_end(end)
    window_start = window_end - timedelta(seconds=seconds)
    caps = retention_caps()
    with get_database().read():  # samples and the retained facts they are described against come from one consistent read
        physiological = physiological_history_store.between(athlete_id, window_start, window_end)
        tracking = tracking_history_store.between(athlete_id, window_start, window_end)
        phys_ret = physiological_history_store.retention(athlete_id)
        track_ret = tracking_history_store.retention(athlete_id)
    return AthleteWindow(
        athlete_id=athlete_id,
        window_start=window_start,
        window_end=window_end,
        physiological_samples=physiological,
        tracking_samples=tracking,
        tracking_rules=TrackingRules(min_confidence=min_confidence),
        availability=WindowAvailability(
            physiological=stream_availability(phys_ret, caps["physiological"], physiological),
            tracking=stream_availability(track_ret, caps["tracking"], tracking),
        ),
    )


@router.get("/{athlete_id}/window", response_model=AthleteWindow)
def get_athlete_window(athlete_id: str, seconds: int = WINDOW_SECONDS, end: AwareDatetime | None = WINDOW_END) -> AthleteWindow:
    """Physiological and tracking samples recorded in the `seconds` before `end` (default: now), oldest to newest, with the applied tracking rules."""
    return select_window(athlete_id, seconds, get_settings().distance_min_confidence, end)


@router.get("/{athlete_id}/summary", response_model=AthleteSummary)
def get_athlete_summary(athlete_id: str, seconds: int = WINDOW_SECONDS, end: AwareDatetime | None = WINDOW_END) -> AthleteSummary:
    """Descriptive counts, heart-rate mean/peak, peak speed, gap-aware distance and high-speed estimates, and timestamps for the `seconds` before `end` (default: now).

    Sample ages are always relative to `window_end`: in a recorded review they mean seconds before the selected end, not age relative to the present.
    """
    settings = get_settings()
    return summarize_window(
        select_window(athlete_id, seconds, settings.distance_min_confidence, end),
        distance_max_gap_seconds=settings.distance_max_gap_seconds,
        distance_min_confidence=settings.distance_min_confidence,
        high_speed_threshold_mps=settings.high_speed_threshold_mps,
    )


# ---- manually selected effort review ----------------------------------------

EFFORT_START = Query(None, description="Selected effort start: ISO 8601 with an explicit timezone, e.g. 2026-10-20T10:00:00Z (encode '+' as %2B). Numeric epochs and naive values are rejected.")
EFFORT_END = Query(None, description="Selected effort end, same format. Must be after effort_start by 1 to 120 seconds and not after backend time.")


@router.get("/{athlete_id}/effort-review", response_model=EffortReview)
def get_effort_review(athlete_id: str, effort_start: str | None = EFFORT_START, effort_end: str | None = EFFORT_END) -> EffortReview:
    """Descriptive review of a manually selected effort: movement over [effort_start, effort_end] and heart rate before, during and after it.

    The caller chooses the effort; nothing is detected or classified. Backend time is read once and caps the after period.
    Movement reuses the summary endpoint's rules. Heart-rate periods are before [start-10 s, start), during [start, end] and
    after (end, end+30 s]; null heart rates stay missing, nothing is interpolated, and only retained samples are reviewed.
    """
    try:
        start = parse_effort_timestamp("effort_start", effort_start)
        end = parse_effort_timestamp("effort_end", effort_end)
        now = clock.utc_now()  # read exactly once per request
        settings = get_settings()
        caps = retention_caps()
        with get_database().read():  # samples and the retained facts in one consistent read
            physiological = physiological_history_store.between(athlete_id, start - timedelta(seconds=BEFORE_SECONDS), end + timedelta(seconds=AFTER_SECONDS))
            tracking = tracking_history_store.between(athlete_id, start, end)
            phys_ret = physiological_history_store.retention(athlete_id)
            track_ret = tracking_history_store.retention(athlete_id)
        return review_effort(
            athlete_id, start, end, now, physiological, tracking,
            distance_max_gap_seconds=settings.distance_max_gap_seconds,
            distance_min_confidence=settings.distance_min_confidence,
            high_speed_threshold_mps=settings.high_speed_threshold_mps,
            physiological_retention=phys_ret, tracking_retention=track_ret, retention_caps=caps,
        )
    except EffortRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---- export of retained samples (JSON Lines interchange format) --------------

JSONL_MEDIA_TYPE = "application/x-ndjson"


@router.get(
    "/{athlete_id}/retained-samples.jsonl",
    responses={200: {"content": {JSONL_MEDIA_TYPE: {}}, "description": "JSON Lines file: one {type, sample} record per line, oldest first"},
               404: {"description": "Neither stream has retained samples for this athlete"}},
)
def export_retained_samples(athlete_id: str) -> Response:
    """Download every sample the engine currently retains for the athlete, both streams, as replayable JSON Lines.

    A snapshot of every retained row (up to each stream's configured cap, whatever it is), read from both tables under one
    lock, sorted by recorded instant with physiological before tracking at equal instants, and written exactly as stored
    (offsets, ids, sources, optional measurements, nulls, zeros, confidences). Nothing is rebased or invented; rows already
    trimmed by a cap are gone. The `X-Athletrix-Physiological-Cap` and `-Tracking-Cap` headers carry the caps in force.
    Not a database backup: no roster, device status, configuration or other athletes.
    """
    captured_at = clock.utc_now()
    caps = retention_caps()
    with get_database().read():  # one consistent read of both streams (the lock is re-entrant for the store reads)
        physiological = physiological_history_store.all(athlete_id)
        tracking = tracking_history_store.all(athlete_id)
    if not physiological and not tracking:
        raise HTTPException(status_code=404, detail=f"No retained samples for athlete '{athlete_id}' in either stream; nothing to export")
    records = export_records(physiological, tracking)
    filename = export_filename(athlete_id, captured_at)
    return Response(
        content=to_jsonl(records),
        media_type=JSONL_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Athletrix-Record-Count": str(len(records)),
            "X-Athletrix-Physiological-Count": str(len(physiological)),
            "X-Athletrix-Tracking-Count": str(len(tracking)),
            "X-Athletrix-Physiological-Cap": str(caps["physiological"]),
            "X-Athletrix-Tracking-Cap": str(caps["tracking"]),
            "Access-Control-Expose-Headers": "Content-Disposition, X-Athletrix-Record-Count, X-Athletrix-Physiological-Count, X-Athletrix-Tracking-Count, X-Athletrix-Physiological-Cap, X-Athletrix-Tracking-Cap",
        },
    )
