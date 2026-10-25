"""Descriptive review of a manually selected effort period. Small pure functions; the API layer does the I/O.

Boundaries (half-open where two periods meet, so no sample is counted twice):
    before  [effort_start - BEFORE_SECONDS, effort_start)
    during  [effort_start, effort_end]
    after   (effort_end, effort_end + AFTER_SECONDS]   capped at the backend time captured for the request

BEFORE_SECONDS and AFTER_SECONDS are engineering defaults for a readable review, not physiological standards.
Heart rate is never interpolated, null heart rates stay missing, movement is never extrapolated, and excluded
tracking intervals are never bridged. Recorded timestamps are used as stored: no device-clock correction and no
automatic estimate of the lag between physical effort and heart-rate response.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.distance import DEFAULT_HIGH_SPEED_THRESHOLD_MPS, DEFAULT_MAX_GAP_SECONDS, DEFAULT_MIN_CONFIDENCE
from app.core.sources import source_breakdown
from app.core.summary import max_inter_sample_gap_seconds, summarize_window
from app.models import AthleteWindow, PhysiologicalSample, TrackingRules, TrackingSample, TrackingSummary
from app.models.effort import EffortReview, EffortRules, HeartRatePeriod, HeartRatePeriods

BEFORE_SECONDS = 10.0     # period immediately before the effort (engineering default)
AFTER_SECONDS = 30.0      # follow-up period after the effort (engineering default)
MIN_EFFORT_SECONDS = 1.0
MAX_EFFORT_SECONDS = 120.0

# ISO 8601 date-time with a 'T' separator and an explicit UTC designator or numeric offset. Numeric epochs and
# naive timestamps do not match and are rejected before any date parsing.
ISO_WITH_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")


class EffortRequestError(ValueError):
    """A request problem the caller must fix (reported as a 422 by the API)."""


def parse_effort_timestamp(name: str, text: str | None) -> datetime:
    """A timezone-aware datetime from an ISO 8601 string with an explicit timezone; EffortRequestError otherwise."""
    if text is None or not text.strip():
        raise EffortRequestError(f"{name} is required: an ISO 8601 timestamp with an explicit timezone, e.g. 2026-10-20T10:00:00Z")
    t = text.strip()
    if re.match(r"^[+-]?\d+(\.\d+)?$", t):
        raise EffortRequestError(f"{name} {t!r} is a numeric value; numeric epochs are not accepted, use ISO 8601 with a timezone, e.g. 2026-10-20T10:00:00Z")
    if not ISO_WITH_TZ.match(t):
        raise EffortRequestError(f"{name} {t!r} must be ISO 8601 with an explicit timezone, e.g. 2026-10-20T10:00:00Z or 2026-10-20T12:00:00+02:00")
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EffortRequestError(f"{name} {t!r} is not a valid date-time ({exc})") from exc


def validate_effort(effort_start: datetime, effort_end: datetime, now: datetime) -> float:
    """Check the selected effort against the limits; return its duration in seconds."""
    if effort_start >= effort_end:
        raise EffortRequestError(f"effort_start {effort_start.isoformat()} must precede effort_end {effort_end.isoformat()}")
    duration = (effort_end - effort_start).total_seconds()
    if duration < MIN_EFFORT_SECONDS or duration > MAX_EFFORT_SECONDS:
        raise EffortRequestError(f"effort duration {duration:.3f} s must be between {MIN_EFFORT_SECONDS:g} and {MAX_EFFORT_SECONDS:g} seconds")
    if effort_end > now:
        raise EffortRequestError(f"effort_end {effort_end.isoformat()} is after backend time {now.isoformat()}; an effort review cannot end in the future")
    return duration


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    start_inclusive: bool
    end_inclusive: bool

    def contains(self, ts: datetime) -> bool:
        after_start = ts >= self.start if self.start_inclusive else ts > self.start
        before_end = ts <= self.end if self.end_inclusive else ts < self.end
        return after_start and before_end


def period_bounds(effort_start: datetime, effort_end: datetime, now: datetime) -> tuple[Period, Period, Period]:
    """The before, during and after periods. `now` caps the after period; adjacent periods never overlap."""
    before = Period(effort_start - timedelta(seconds=BEFORE_SECONDS), effort_start, True, False)
    during = Period(effort_start, effort_end, True, True)
    after_end = min(effort_end + timedelta(seconds=AFTER_SECONDS), now)
    after = Period(effort_end, after_end, False, True)
    return before, during, after


def follow_up_elapsed(effort_end: datetime, now: datetime) -> bool:
    """Whether the whole after period lies at or before `now`. Says nothing about which samples exist."""
    return effort_end + timedelta(seconds=AFTER_SECONDS) <= now


def select_period(samples: Sequence[PhysiologicalSample], period: Period) -> list[PhysiologicalSample]:
    """The samples whose recorded timestamp lies in the period (compared as instants), in their given order."""
    return [s for s in samples if period.contains(s.timestamp)]


def summarize_heart_rate_period(samples: Sequence[PhysiologicalSample], period: Period) -> HeartRatePeriod:
    """Descriptive heart-rate figures for the samples in `period`. Null heart rates count as samples only."""
    chosen = sorted(select_period(samples, period), key=lambda s: s.timestamp)
    stamps = [s.timestamp for s in chosen]
    rates = [s.heart_rate_bpm for s in chosen if s.heart_rate_bpm is not None]
    return HeartRatePeriod(
        period_start=period.start,
        period_end=period.end,
        start_inclusive=period.start_inclusive,
        end_inclusive=period.end_inclusive,
        sample_count=len(chosen),
        heart_rate_count=len(rates),
        heart_rate_mean_bpm=sum(rates) / len(rates) if rates else None,
        heart_rate_peak_bpm=max(rates) if rates else None,
        first_sample_at=stamps[0] if stamps else None,
        last_sample_at=stamps[-1] if stamps else None,
        max_inter_sample_gap_seconds=max_inter_sample_gap_seconds(stamps),
        sources=source_breakdown(chosen),
    )


def summarize_movement(
    athlete_id: str,
    effort_start: datetime,
    effort_end: datetime,
    tracking: Sequence[TrackingSample],
    *,
    distance_max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    distance_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    high_speed_threshold_mps: float = DEFAULT_HIGH_SPEED_THRESHOLD_MPS,
) -> TrackingSummary:
    """The existing tracking summary over [effort_start, effort_end]; the window fraction is relative to the effort length."""
    window = AthleteWindow(
        athlete_id=athlete_id,
        window_start=effort_start,
        window_end=effort_end,
        physiological_samples=[],
        tracking_samples=[s for s in tracking if effort_start <= s.timestamp <= effort_end],
        tracking_rules=TrackingRules(min_confidence=distance_min_confidence),
    )
    return summarize_window(
        window,
        distance_max_gap_seconds=distance_max_gap_seconds,
        distance_min_confidence=distance_min_confidence,
        high_speed_threshold_mps=high_speed_threshold_mps,
    ).tracking


def review_effort(
    athlete_id: str,
    effort_start: datetime,
    effort_end: datetime,
    now: datetime,
    physiological: Sequence[PhysiologicalSample],
    tracking: Sequence[TrackingSample],
    *,
    distance_max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    distance_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    high_speed_threshold_mps: float = DEFAULT_HIGH_SPEED_THRESHOLD_MPS,
) -> EffortReview:
    """Assemble the review from already-validated bounds and the retained samples that may fall in any period."""
    duration = validate_effort(effort_start, effort_end, now)
    before, during, after = period_bounds(effort_start, effort_end, now)
    return EffortReview(
        athlete_id=athlete_id,
        effort_start=effort_start,
        effort_end=effort_end,
        effort_duration_seconds=duration,
        captured_at=now,
        follow_up_elapsed=follow_up_elapsed(effort_end, now),
        rules=EffortRules(
            before_seconds=BEFORE_SECONDS, after_seconds=AFTER_SECONDS,
            min_effort_seconds=MIN_EFFORT_SECONDS, max_effort_seconds=MAX_EFFORT_SECONDS,
        ),
        movement=summarize_movement(
            athlete_id, effort_start, effort_end, tracking,
            distance_max_gap_seconds=distance_max_gap_seconds,
            distance_min_confidence=distance_min_confidence,
            high_speed_threshold_mps=high_speed_threshold_mps,
        ),
        heart_rate=HeartRatePeriods(
            before=summarize_heart_rate_period(physiological, before),
            during=summarize_heart_rate_period(physiological, during),
            after=summarize_heart_rate_period(physiological, after),
        ),
    )
