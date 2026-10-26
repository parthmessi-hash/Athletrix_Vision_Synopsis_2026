from pydantic import AwareDatetime, Field, field_validator

from app.models.common import AthletrixModel


class PeriodCreate(AthletrixModel):
    """Request to save a manually selected effort period of a stopped recording: a short label and the exact bounds.

    The bounds are ISO 8601 timestamps with an explicit timezone and follow the effort review's rules (1 to 120 s,
    not after backend time). Nothing is detected or classified: the person chose the period.
    """

    label: str = Field(..., min_length=1, max_length=80, description="Short user-provided label, e.g. 'second sprint'. Not an identifier: labels may repeat.")
    effort_start: str = Field(..., description="Selected start, ISO 8601 with an explicit timezone.")
    effort_end: str = Field(..., description="Selected end, ISO 8601 with an explicit timezone; 1 to 120 s after the start.")

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("label must not be blank")
        return v.strip()


class SavedPeriod(AthletrixModel):
    """A saved effort selection within a stopped recording: the selection only, never a frozen calculation.

    The bounds are stored as the explicit instants that were sent (with their offsets) and are never rebased. A
    review of the period is calculated when asked for, with the engine's rules in force at that time; nothing here
    says whether the phases hold measurements, and an empty or partial phase is a valid outcome of a review.
    """

    id: str = Field(..., description="Generated id, e.g. per-20261026T200000Z-1a2b3c.")
    recording_id: str
    athlete_id: str
    label: str
    effort_start: AwareDatetime = Field(..., description="Selected start, as sent (offset kept).")
    effort_end: AwareDatetime = Field(..., description="Selected end, as sent (offset kept).")
    duration_seconds: float = Field(..., gt=0)
    created_at: AwareDatetime = Field(..., description="Backend UTC time when the period was saved.")
