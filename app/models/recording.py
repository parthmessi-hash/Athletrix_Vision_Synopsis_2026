from typing import Literal

from pydantic import AwareDatetime, Field, field_validator

from app.models.athlete import Athlete
from app.models.common import AthletrixModel
from app.models.effort import EffortReview
from app.models.summary import AthleteSummary
from app.models.window import AthleteWindow


class RecordingStart(AthletrixModel):
    """Request to start a local recording: a name and the registered athletes it will cover."""

    name: str = Field(..., min_length=1, max_length=120, description="User-provided name, e.g. 'Tuesday practice'.")
    athlete_ids: list[str] = Field(..., min_length=1, description="Registered athlete ids to record; fixed for the recording's lifetime.")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()

    @field_validator("athlete_ids")
    @classmethod
    def _ids_distinct_and_non_empty(cls, v: list[str]) -> list[str]:
        if any(not a.strip() for a in v):
            raise ValueError("athlete ids must not be blank")
        if len(set(v)) != len(v):
            raise ValueError("athlete ids must be distinct")
        return v


class Recording(AthletrixModel):
    """An explicit local recording: which athletes it covers and when it ran, on the backend's clock.

    Accepted samples for the selected athletes are preserved in session-specific storage while the recording is
    active, independently of the rolling retention caps. Membership is decided at ingestion time (accepted while
    active), not by the sample's own timestamp. `athletes` is a snapshot of the roster taken at the start, kept with
    the recording even if the registry changes later. An active recording survives a backend restart and stays
    active until it is explicitly stopped. Nothing is uploaded anywhere and nothing is deleted automatically.
    """

    id: str = Field(..., description="Generated id, e.g. rec-20261026T200000Z-1a2b3c.")
    name: str = Field(..., description="User-provided name.")
    status: Literal["active", "stopped"] = Field(..., description="active until stopped explicitly.")
    started_at: AwareDatetime = Field(..., description="Backend UTC time when the recording was started.")
    stopped_at: AwareDatetime | None = Field(None, description="Backend UTC time when it was stopped; null while active.")
    athlete_ids: list[str] = Field(..., description="Selected athlete ids, in the order given at the start.")
    athletes: list[Athlete] = Field(..., description="Roster snapshot of the selected athletes as registered at the start.")


class RecordedStream(AthletrixModel):
    """Count and recorded-timestamp bounds of one athlete's samples in one stream of a recording. Not coverage."""

    sample_count: int = Field(..., ge=0)
    earliest_timestamp: AwareDatetime | None = Field(None, description="Recorded timestamp of the oldest recorded sample (UTC); null if none.")
    latest_timestamp: AwareDatetime | None = Field(None, description="Recorded timestamp of the newest recorded sample (UTC); null if none.")


class RecordedAthlete(AthletrixModel):
    """Per athlete of a recording: what was recorded per stream."""

    athlete: Athlete
    physiological: RecordedStream
    tracking: RecordedStream


class RecordingProvenance(AthletrixModel):
    """Where an imported recording came from: the original recording id, the package and the id mapping of its saved periods."""

    source_recording_id: str
    package_sha256: str
    package_format: str
    package_generated_at: str
    imported_at: AwareDatetime
    period_id_map: dict[str, str] = Field(..., description="Original saved-period id -> id on this engine.")


class RecordingDetail(Recording):
    """A recording with its per-athlete recorded counts and bounds (the samples accepted for it, as stored)."""

    recorded: list[RecordedAthlete] = Field(..., description="One entry per selected athlete, in roster order.")
    imported: RecordingProvenance | None = Field(None, description="Import provenance when the recording came from a portable package; null for one recorded on this engine.")


class ActiveRecording(AthletrixModel):
    """The recording currently active on this backend, or null."""

    recording: Recording | None = None


class RecordingRef(AthletrixModel):
    """Which recording a session response belongs to, with its own start and stop times (the backend's clock).

    These are when the recording ran, not when the samples were measured: the samples carry their own recorded
    timestamps, and a request's analysis period is a third, separate thing.
    """

    id: str
    name: str
    status: Literal["active", "stopped"]
    started_at: AwareDatetime
    stopped_at: AwareDatetime | None = None


class SessionStreamAvailability(AthletrixModel):
    """What one recording holds for an athlete in one stream, against what a request selected from it.

    No retention cap applies to a recording, so there is none here. `recorded_*` describe the rows the recording
    holds now; `selected_*` describe the samples the request chose for its period (the count equals the number of
    samples in the accompanying result). Bounds are not coverage, and an absence says nothing about why.
    """

    recorded_count: int = Field(..., ge=0)
    recorded_earliest: AwareDatetime | None = None
    recorded_latest: AwareDatetime | None = None
    selected_count: int = Field(..., ge=0)
    selected_first: AwareDatetime | None = None
    selected_last: AwareDatetime | None = None


class SessionWindowAvailability(AthletrixModel):
    physiological: SessionStreamAvailability
    tracking: SessionStreamAvailability


class SessionHeartRateAvailability(AthletrixModel):
    before: SessionStreamAvailability
    during: SessionStreamAvailability
    after: SessionStreamAvailability


class SessionEffortAvailability(AthletrixModel):
    movement: SessionStreamAvailability
    heart_rate: SessionHeartRateAvailability


class RecordedData(AthletrixModel):
    """Per stream: count and recorded-timestamp bounds of one athlete's samples in one recording."""

    recording: RecordingRef
    athlete: Athlete
    physiological: RecordedStream
    tracking: RecordedStream


class RecordingWindow(AthleteWindow):
    """A window over one recording's samples only. `availability` (rolling caps) is null; see `session_availability`."""

    recording: RecordingRef
    session_availability: SessionWindowAvailability


class RecordingSummary(AthleteSummary):
    """The summary over one recording's samples only, same calculations and rules as the rolling summary."""

    recording: RecordingRef
    session_availability: SessionWindowAvailability


class AppliedDistanceRules(AthletrixModel):
    """The engine settings applied to every movement figure of a batch review (adjustable engineering defaults, not sport standards)."""

    max_gap_seconds: float
    min_confidence: float
    high_speed_threshold_mps: float


class RecordingEffortReview(EffortReview):
    """An effort review over one recording's samples only, same calculations, boundaries and rules as the rolling review."""

    recording: RecordingRef
    session_availability: SessionEffortAvailability


class PeriodReviewItem(AthletrixModel):
    """One saved period with its review, calculated in the batch."""

    period: "SavedPeriod"
    review: RecordingEffortReview


class PeriodBatchReview(AthletrixModel):
    """Descriptive reviews of one athlete's saved periods in one stopped recording, calculated together.

    One captured time and one set of engine settings apply to every item; the samples come from one consistent read of
    the recording. Items are listed by start instant, never ranked. Periods differ in length and in what happened, so
    their figures are not comparable as equivalents, and nothing here judges fatigue, recovery, readiness or safety.
    """

    recording: RecordingRef
    athlete: Athlete
    captured_at: AwareDatetime = Field(..., description="Backend time read once for the whole batch; caps every after period.")
    rules: "EffortRules" = Field(..., description="Period lengths and effort limits applied to every item.")
    distance_rules: AppliedDistanceRules = Field(..., description="Engine settings applied to every movement figure.")
    items: list[PeriodReviewItem] = Field(..., description="One per saved period, ordered by start instant.")
    note: str = "Current calculations over recorded samples with the rules above; a saved period stores only its selection, so other engine settings give other figures. Periods are listed, not ranked."


from app.models.effort import EffortRules  # noqa: E402
from app.models.period import SavedPeriod  # noqa: E402

PeriodReviewItem.model_rebuild()
PeriodBatchReview.model_rebuild()
