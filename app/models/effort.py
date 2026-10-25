from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel
from app.models.sources import SourceBreakdown
from app.models.summary import TrackingSummary


class EffortRules(AthletrixModel):
    """The rules applied to an effort review: adjustable engineering defaults, not physiological standards.

    The before and after periods are fixed lengths chosen for a readable review (10 s and 30 s). They are
    not validated recovery or warm-up intervals and carry no physiological meaning of their own. The
    movement rules (gap, confidence, high-speed threshold) are the same ones the summary endpoint applies.
    """

    before_seconds: float = Field(..., gt=0, description="Length of the period reviewed immediately before effort_start, seconds.")
    after_seconds: float = Field(..., gt=0, description="Length of the follow-up period after effort_end, seconds (capped at backend time).")
    min_effort_seconds: float = Field(..., gt=0, description="Shortest accepted effort, seconds.")
    max_effort_seconds: float = Field(..., gt=0, description="Longest accepted effort, seconds.")


class HeartRatePeriod(AthletrixModel):
    """Descriptive heart-rate figures for one period, over the physiological samples whose recorded timestamp lies in it.

    `period_start`/`period_end` are the bounds actually applied; `start_inclusive`/`end_inclusive` say which
    boundaries belong to the period, so adjacent periods never share a sample. Null heart rates count as
    samples but not as measurements and are never replaced by 0. The mean is the plain arithmetic average of
    the measurements present (not time-weighted) and nothing is interpolated between samples. Empty periods
    report zero counts and null measurements. The largest gap is measured between the samples that were
    present; it does not show that the period was covered. These are descriptions of recorded samples only:
    a difference between periods is not a recovery rate, a baseline comparison, a fatigue label or a diagnosis.
    """

    period_start: AwareDatetime = Field(..., description="Applied start of the period.")
    period_end: AwareDatetime = Field(..., description="Applied end of the period.")
    start_inclusive: bool = Field(..., description="Whether a sample timestamped exactly at period_start belongs to this period.")
    end_inclusive: bool = Field(..., description="Whether a sample timestamped exactly at period_end belongs to this period.")
    sample_count: int = Field(..., ge=0, description="Physiological samples in the period.")
    heart_rate_count: int = Field(..., ge=0, description="Samples with a non-null heart_rate_bpm.")
    heart_rate_mean_bpm: float | None = Field(None, description="Arithmetic mean of non-null heart rates; null if none.")
    heart_rate_peak_bpm: float | None = Field(None, description="Highest non-null heart rate; null if none.")
    first_sample_at: AwareDatetime | None = Field(None, description="Oldest sample timestamp in the period; null if none.")
    last_sample_at: AwareDatetime | None = Field(None, description="Newest sample timestamp in the period; null if none.")
    max_inter_sample_gap_seconds: float | None = Field(None, ge=0, description="Largest gap between consecutive samples, seconds; null if fewer than two.")
    sources: SourceBreakdown = Field(default_factory=SourceBreakdown, description="Reported sources of the samples in the period (adds up to sample_count).")


class HeartRatePeriods(AthletrixModel):
    """The three non-overlapping heart-rate periods around a selected effort."""

    before: HeartRatePeriod = Field(..., description="[effort_start - before_seconds, effort_start): the seconds immediately before the effort.")
    during: HeartRatePeriod = Field(..., description="[effort_start, effort_end]: the selected effort itself.")
    after: HeartRatePeriod = Field(..., description="(effort_end, effort_end + after_seconds], capped at the captured backend time.")


class EffortReview(AthletrixModel):
    """Descriptive review of a manually selected effort period for one athlete.

    The effort bounds are chosen by the caller; nothing here detects sprints or classifies efforts. Movement
    figures are the existing tracking summary over [effort_start, effort_end] (same gap, confidence and
    high-speed rules as the summary endpoint; excluded intervals are never bridged and nothing is
    extrapolated). Heart rate is described for three non-overlapping periods. Recorded timestamps are used as
    stored, without device-clock correction between streams and without any automatic estimate of the lag
    between physical effort and heart-rate response. Only retained samples (newest 300 per athlete per
    stream) can be reviewed, so any period may be partially populated or empty.

    `follow_up_elapsed` only says whether the full after-period lies at or before the captured backend time.
    It does not say that samples for that period were received, retained or complete.
    """

    athlete_id: str = Field(..., description="Athlete the review belongs to.")
    effort_start: AwareDatetime = Field(..., description="Selected effort start, as requested.")
    effort_end: AwareDatetime = Field(..., description="Selected effort end, as requested.")
    effort_duration_seconds: float = Field(..., gt=0, description="effort_end minus effort_start, seconds.")
    captured_at: AwareDatetime = Field(..., description="Backend time read once for this request; caps the after period.")
    follow_up_elapsed: bool = Field(..., description="True when effort_end + after_seconds is at or before captured_at. Says nothing about sample availability.")
    rules: EffortRules = Field(..., description="Applied period lengths and effort limits.")
    movement: TrackingSummary = Field(..., description="Tracking summary over [effort_start, effort_end], with its distance rules and source breakdown.")
    heart_rate: HeartRatePeriods = Field(..., description="Heart-rate descriptions for the before, during and after periods.")
