from pydantic import AwareDatetime, Field

from app.models.availability import WindowAvailability
from app.models.common import AthletrixModel
from app.models.sources import SourceBreakdown


class PhysiologicalSummary(AthletrixModel):
    """Descriptive statistics over the physiological samples in a window.

    Units: counts are sample counts; heart-rate values are beats per minute;
    age and gap values are seconds derived from recorded sample timestamps.
    The maximum gap is measured only between consecutive samples that were
    available; it does not show that the whole requested window was covered.
    The mean is the plain arithmetic average of the heart-rate measurements
    received (null heart rates are ignored), not a time-weighted average, so
    uneven sampling or gaps are not corrected for.
    """

    sample_count: int = Field(..., ge=0, description="Physiological samples in the window.")
    heart_rate_count: int = Field(..., ge=0, description="Samples with a non-null heart_rate_bpm.")
    heart_rate_mean_bpm: float | None = Field(None, description="Arithmetic mean of non-null heart rates; null if none.")
    heart_rate_peak_bpm: float | None = Field(None, description="Highest non-null heart rate; null if none.")
    first_sample_at: AwareDatetime | None = Field(None, description="Oldest sample timestamp in the window; null if none.")
    last_sample_at: AwareDatetime | None = Field(None, description="Newest sample timestamp in the window; null if none.")
    latest_sample_age_seconds: float | None = Field(
        None, ge=0, description="window_end minus the newest sample timestamp, seconds; null if no samples."
    )
    max_inter_sample_gap_seconds: float | None = Field(
        None, ge=0, description="Largest gap between consecutive sample timestamps, seconds; null if fewer than two samples."
    )
    sources: SourceBreakdown = Field(default_factory=SourceBreakdown, description="Reported sources of the samples in the window (adds up to sample_count).")


CONTINUITY_RULE = "never join consecutive samples whose segment_id differs"


class DistanceRules(AthletrixModel):
    """Settings applied to the distance estimate (adjustable engineering defaults, not sport standards)."""

    max_gap_seconds: float = Field(..., gt=0, description="Longest consecutive-sample interval included, seconds.")
    min_confidence: float = Field(..., ge=0, le=1, description="Minimum tracking_confidence required at both ends of an interval.")
    high_speed_threshold_mps: float = Field(..., gt=0, description="Speed at or above which time and distance count as high-speed, m/s.")
    continuity_rule: str = Field(CONTINUITY_RULE, description="Always applied; streams without segment_id are unaffected.")


class HighSpeedEstimate(AthletrixModel):
    """Time and distance at or above a speed threshold, over the same eligible intervals as the distance estimate.

    Speed is assumed to change linearly within each eligible interval; when an interval crosses the
    threshold the crossing time is interpolated and only the qualifying part counts. An interval held
    exactly at the threshold counts in full; one that only touches it at an endpoint contributes zero.
    Both values are null when no interval was eligible and real zeros when eligible intervals existed
    but none reached the threshold. Duration never exceeds the eligible duration and distance never
    exceeds the total estimated distance. Descriptive workload estimates only: not sprint detection,
    not a fatigue classification, not a substitution recommendation.

    Units: seconds, metres, metres per second. The default threshold is a demonstration value, not a
    validated standard for every sport or athlete.
    """

    high_speed_duration_seconds: float | None = Field(None, ge=0, description="Seconds at or above the threshold within eligible intervals; null if none eligible.")
    high_speed_distance_m: float | None = Field(None, ge=0, description="Metres covered at or above the threshold within eligible intervals; null if none eligible.")
    high_speed_threshold_mps: float = Field(..., gt=0, description="Applied threshold, m/s.")


class DistanceEstimate(AthletrixModel):
    """Gap-aware distance estimate over eligible consecutive tracking intervals.

    An interval between two consecutive samples is eligible when its elapsed time is positive and at
    most `max_gap_seconds`, both endpoint confidences are at least `min_confidence` and both samples carry the same
    `segment_id` (or neither carries one: a provider-declared discontinuity is never joined). Each eligible
    interval contributes (v_prev + v_cur) / 2 * elapsed, which assumes speed changes linearly between
    samples. Excluded intervals contribute nothing and are never bridged; nothing is extrapolated
    outside the samples. This is an estimate over the eligible part of the window, not a guaranteed
    full-window distance.

    Units: metres, seconds; the fraction is included duration divided by the requested window length.
    """

    estimated_distance_m: float | None = Field(None, ge=0, description="Sum over eligible intervals, metres; null if no interval was eligible; 0.0 when eligible intervals were stationary.")
    included_duration_seconds: float = Field(..., ge=0, description="Total elapsed time of eligible intervals, seconds (0 if none).")
    included_window_fraction: float = Field(..., ge=0, le=1, description="included_duration_seconds divided by the requested window length (0 if none).")
    excluded_interval_count: int = Field(..., ge=0, description="Consecutive-sample intervals rejected by the gap, confidence or continuity rule, each counted once.")
    continuity_break_count: int = Field(0, ge=0, description="Of the excluded intervals, those whose endpoints carry different segment_id values (a provider-declared discontinuity).")
    max_gap_seconds: float = Field(..., gt=0, description="Applied rule: longest interval included, seconds.")
    min_confidence: float = Field(..., ge=0, le=1, description="Applied rule: minimum tracking_confidence at both ends of an interval.")


class TrackingSummary(AthletrixModel):
    """Descriptive statistics over the tracking samples in a window.

    Units: counts are sample counts; speed is metres per second; age and gap
    values are seconds derived from recorded sample timestamps. A peak of
    0.0 is a real reading (the athlete was stationary in every sample). The
    maximum gap is measured only between consecutive samples that were
    available; it does not show that the whole requested window was covered.
    """

    sample_count: int = Field(..., ge=0, description="Tracking samples in the window.")
    speed_peak_mps: float | None = Field(None, description="Highest speed_mps; null if no samples.")
    first_sample_at: AwareDatetime | None = Field(None, description="Oldest sample timestamp in the window; null if none.")
    last_sample_at: AwareDatetime | None = Field(None, description="Newest sample timestamp in the window; null if none.")
    latest_sample_age_seconds: float | None = Field(
        None, ge=0, description="window_end minus the newest sample timestamp, seconds; null if no samples."
    )
    max_inter_sample_gap_seconds: float | None = Field(
        None, ge=0, description="Largest gap between consecutive sample timestamps, seconds; null if fewer than two samples."
    )
    estimated_distance_m: float | None = Field(None, ge=0, description="See DistanceEstimate.estimated_distance_m.")
    included_duration_seconds: float = Field(0.0, ge=0, description="See DistanceEstimate.included_duration_seconds.")
    included_window_fraction: float = Field(0.0, ge=0, le=1, description="See DistanceEstimate.included_window_fraction.")
    excluded_interval_count: int = Field(0, ge=0, description="See DistanceEstimate.excluded_interval_count.")
    continuity_break_count: int = Field(0, ge=0, description="See DistanceEstimate.continuity_break_count.")
    high_speed_duration_seconds: float | None = Field(None, ge=0, description="See HighSpeedEstimate.high_speed_duration_seconds.")
    high_speed_distance_m: float | None = Field(None, ge=0, description="See HighSpeedEstimate.high_speed_distance_m.")
    distance_rules: DistanceRules | None = Field(None, description="The gap, confidence and high-speed threshold rules applied to the estimates.")
    sources: SourceBreakdown = Field(default_factory=SourceBreakdown, description="Reported sources of the samples in the window (adds up to sample_count).")


class AthleteSummary(AthletrixModel):
    """Descriptive metrics for one athlete over a recent window.

    These describe the samples that were available in memory for the window.
    Gaps in transmission and the per-stream history caps can mean the window
    is only partially covered. No scoring, baseline or recovery inference is
    made here.
    """

    athlete_id: str = Field(..., description="Athlete the summary belongs to.")
    window_start: AwareDatetime = Field(..., description="Inclusive start of the window (UTC).")
    window_end: AwareDatetime = Field(..., description="Inclusive end of the window: backend time at the request (UTC).")
    physiological: PhysiologicalSummary
    tracking: TrackingSummary
    availability: WindowAvailability | None = Field(None, description="Per stream: the configured cap, what is retained for the athlete and what this window selected (selected_count equals each stream's sample_count). Null when not computed.")
