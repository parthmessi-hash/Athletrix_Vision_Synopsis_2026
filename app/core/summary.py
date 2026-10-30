"""Descriptive summary of an AthleteWindow. Pure functions, no I/O."""

from collections.abc import Sequence
from datetime import datetime

from app.core.distance import (
    DEFAULT_HIGH_SPEED_THRESHOLD_MPS,
    DEFAULT_MAX_GAP_SECONDS,
    DEFAULT_MIN_CONFIDENCE,
    estimate_distance,
    estimate_high_speed,
)
from app.core.sources import source_breakdown
from app.models import AthleteSummary, AthleteWindow, DistanceRules, PhysiologicalSummary, TrackingSummary


def latest_sample_age_seconds(timestamps: Sequence[datetime], window_end: datetime) -> float | None:
    """Seconds from the newest timestamp to window_end; None if no samples. Timestamps are sorted ascending."""
    return (window_end - timestamps[-1]).total_seconds() if timestamps else None


def max_inter_sample_gap_seconds(timestamps: Sequence[datetime]) -> float | None:
    """Largest gap between consecutive timestamps; None if fewer than two. Timestamps are sorted ascending."""
    if len(timestamps) < 2:
        return None
    return max((b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:]))


def summarize_window(
    window: AthleteWindow,
    *,
    distance_max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    distance_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    high_speed_threshold_mps: float = DEFAULT_HIGH_SPEED_THRESHOLD_MPS,
) -> AthleteSummary:
    phys = window.physiological_samples
    track = window.tracking_samples

    phys_ts = [s.timestamp for s in phys]
    heart_rates = [s.heart_rate_bpm for s in phys if s.heart_rate_bpm is not None]
    physiological = PhysiologicalSummary(
        sample_count=len(phys),
        heart_rate_count=len(heart_rates),
        heart_rate_mean_bpm=sum(heart_rates) / len(heart_rates) if heart_rates else None,
        heart_rate_peak_bpm=max(heart_rates) if heart_rates else None,
        first_sample_at=phys[0].timestamp if phys else None,
        last_sample_at=phys[-1].timestamp if phys else None,
        latest_sample_age_seconds=latest_sample_age_seconds(phys_ts, window.window_end),
        max_inter_sample_gap_seconds=max_inter_sample_gap_seconds(phys_ts),
        sources=source_breakdown(phys),  # every selected sample counts, null heart rates included
    )

    track_ts = [s.timestamp for s in track]
    speeds = [s.speed_mps for s in track]
    window_seconds = (window.window_end - window.window_start).total_seconds()
    dist = estimate_distance(track, window_seconds, distance_max_gap_seconds, distance_min_confidence)
    hs = estimate_high_speed(track, high_speed_threshold_mps, distance_max_gap_seconds, distance_min_confidence)
    tracking = TrackingSummary(
        sample_count=len(track),
        speed_peak_mps=max(speeds) if speeds else None,  # max() of zeros is 0.0, kept as a real value
        first_sample_at=track[0].timestamp if track else None,
        last_sample_at=track[-1].timestamp if track else None,
        latest_sample_age_seconds=latest_sample_age_seconds(track_ts, window.window_end),
        max_inter_sample_gap_seconds=max_inter_sample_gap_seconds(track_ts),
        estimated_distance_m=dist.estimated_distance_m,
        included_duration_seconds=dist.included_duration_seconds,
        included_window_fraction=dist.included_window_fraction,
        excluded_interval_count=dist.excluded_interval_count,
        continuity_break_count=dist.continuity_break_count,
        predicted_interval_count=dist.predicted_interval_count,
        high_speed_duration_seconds=hs.high_speed_duration_seconds,
        high_speed_distance_m=hs.high_speed_distance_m,
        distance_rules=DistanceRules(
            max_gap_seconds=dist.max_gap_seconds,
            min_confidence=dist.min_confidence,
            high_speed_threshold_mps=hs.high_speed_threshold_mps,
        ),
        sources=source_breakdown(track),
    )

    return AthleteSummary(
        athlete_id=window.athlete_id,
        window_start=window.window_start,
        window_end=window.window_end,
        physiological=physiological,
        tracking=tracking,
        availability=window.availability,   # the same facts the window was selected under; selected counts equal the sample counts above
    )
