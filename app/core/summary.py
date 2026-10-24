"""Descriptive summary of an AthleteWindow. Pure functions, no I/O."""

from app.models import AthleteSummary, AthleteWindow, PhysiologicalSummary, TrackingSummary


def summarize_window(window: AthleteWindow) -> AthleteSummary:
    phys = window.physiological_samples
    track = window.tracking_samples

    heart_rates = [s.heart_rate_bpm for s in phys if s.heart_rate_bpm is not None]
    physiological = PhysiologicalSummary(
        sample_count=len(phys),
        heart_rate_count=len(heart_rates),
        heart_rate_mean_bpm=sum(heart_rates) / len(heart_rates) if heart_rates else None,
        heart_rate_peak_bpm=max(heart_rates) if heart_rates else None,
        first_sample_at=phys[0].timestamp if phys else None,
        last_sample_at=phys[-1].timestamp if phys else None,
    )

    speeds = [s.speed_mps for s in track]
    tracking = TrackingSummary(
        sample_count=len(track),
        speed_peak_mps=max(speeds) if speeds else None,  # max() of zeros is 0.0, kept as a real value
        first_sample_at=track[0].timestamp if track else None,
        last_sample_at=track[-1].timestamp if track else None,
    )

    return AthleteSummary(
        athlete_id=window.athlete_id,
        window_start=window.window_start,
        window_end=window.window_end,
        physiological=physiological,
        tracking=tracking,
    )
