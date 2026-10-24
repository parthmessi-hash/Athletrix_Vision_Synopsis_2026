from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel


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
