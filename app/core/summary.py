"""Descriptive summary of an AthleteWindow. Pure functions, no I/O."""

from collections.abc import Sequence
from datetime import datetime

from app.models import AthleteSummary, AthleteWindow, PhysiologicalSummary, TrackingSummary


def latest_sample_age_seconds(timestamps: Sequence[datetime], window_end: datetime) -> float | None:
    """Seconds from the newest timestamp to window_end; None if no samples. Timestamps are sorted ascending."""
    return (window_end - timestamps[-1]).total_seconds() if timestamps else None


def max_inter_sample_gap_seconds(timestamps: Sequence[datetime]) -> float | None:
    """Largest gap between consecutive timestamps; None if fewer than two. Timestamps are sorted ascending."""
    if len(timestamps) < 2:
        return None
    return max((b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:]))


def summarize_window(window: AthleteWindow) -> AthleteSummary:
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
    )

    track_ts = [s.timestamp for s in track]
    speeds = [s.speed_mps for s in track]
    tracking = TrackingSummary(
        sample_count=len(track),
        speed_peak_mps=max(speeds) if speeds else None,  # max() of zeros is 0.0, kept as a real value
        first_sample_at=track[0].timestamp if track else None,
        last_sample_at=track[-1].timestamp if track else None,
        latest_sample_age_seconds=latest_sample_age_seconds(track_ts, window.window_end),
        max_inter_sample_gap_seconds=max_inter_sample_gap_seconds(track_ts),
    )

    return AthleteSummary(
        athlete_id=window.athlete_id,
        window_start=window.window_start,
        window_end=window.window_end,
        physiological=physiological,
        tracking=tracking,
    )
