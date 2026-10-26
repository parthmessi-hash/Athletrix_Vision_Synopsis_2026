from typing import Literal

from pydantic import AwareDatetime, Field, field_validator

from app.models.athlete import Athlete
from app.models.common import AthletrixModel


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


class RecordingDetail(Recording):
    """A recording with its per-athlete recorded counts and bounds (the samples accepted for it, as stored)."""

    recorded: list[RecordedAthlete] = Field(..., description="One entry per selected athlete, in roster order.")


class ActiveRecording(AthletrixModel):
    """The recording currently active on this backend, or null."""

    recording: Recording | None = None
