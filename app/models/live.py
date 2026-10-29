from pydantic import AwareDatetime, Field

from app.models.athlete import Athlete
from app.models.common import AthletrixModel, SampleReceipt
from app.models.physiological import PhysiologicalSample
from app.models.team import ReportedDevice
from app.models.tracking import TrackingSample


class AthleteLive(AthletrixModel):
    """One athlete's coherent live snapshot: the same selection the team overview uses, at one captured instant.

    The newest retained sample per stream at or before `captured_at` is chosen; a sample timestamped after that
    instant (accepted within the ingestion clock-skew allowance) stays stored but is not shown as current until
    backend time reaches it, and `*_newest_ahead_seconds` says how far ahead such a sample is. Ages are
    `captured_at` minus the sample timestamp, never negative. Registration is not required for ingestion, so an
    unregistered athlete id is answered with `registered: false`, `athlete: null` and whatever samples it has; an
    id with nothing stored simply has null samples. The plain `latest` endpoints keep their newest-stored
    semantics. Nothing here judges freshness.
    """

    athlete_id: str = Field(..., description="Athlete id as requested.")
    captured_at: AwareDatetime = Field(..., description="Backend time read once for this snapshot (UTC).")
    registered: bool = Field(..., description="Whether the id is in the athlete registry.")
    athlete: Athlete | None = Field(None, description="Registered identity, or null when not registered.")
    physiological: PhysiologicalSample | None = Field(None, description="Newest retained physiological sample at or before captured_at; null if none.")
    tracking: TrackingSample | None = Field(None, description="Newest retained tracking sample at or before captured_at; null if none.")
    physiological_age_seconds: float | None = Field(None, ge=0, description="captured_at minus the physiological sample timestamp; null if no sample.")
    tracking_age_seconds: float | None = Field(None, ge=0, description="captured_at minus the tracking sample timestamp; null if no sample.")
    physiological_newest_ahead_seconds: float | None = Field(None, gt=0, description="How far after captured_at the newest stored physiological sample is, when it is not yet current; null otherwise.")
    tracking_newest_ahead_seconds: float | None = Field(None, gt=0, description="How far after captured_at the newest stored tracking sample is, when it is not yet current; null otherwise.")
    devices: list[ReportedDevice] = Field(default_factory=list, description="Device ids named by the chosen samples with their latest status reports (same rules as the team overview).")
    physiological_receipt: SampleReceipt | None = Field(None, description="When this engine received the chosen physiological sample; null if none or not recorded.")
    tracking_receipt: SampleReceipt | None = Field(None, description="When this engine received the chosen tracking sample; null if none or not recorded.")
