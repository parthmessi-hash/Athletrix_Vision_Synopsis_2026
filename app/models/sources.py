from pydantic import Field, model_validator

from app.models.common import AthletrixModel, SampleSource


class SourceBreakdown(AthletrixModel):
    """How many of one stream's selected samples reported each source, and how many reported none.

    `reported` maps each generic source category that occurs to its count (categories
    with zero samples are omitted); `not_reported` counts samples with no `source`
    field (older tracking records, or senders that did not say). The two always add
    up to the stream's sample count. A source is metadata supplied by whoever sent
    the sample: it is a label, not proof of where the data came from or of sensor
    accuracy, and it is never inferred from file names, values or delivery path.
    """

    reported: dict[SampleSource, int] = Field(default_factory=dict, description="Count per reported source category; only categories that occur.")
    not_reported: int = Field(0, ge=0, description="Samples whose source was not reported.")

    @model_validator(mode="after")
    def _counts_positive(self) -> "SourceBreakdown":
        bad = [k for k, v in self.reported.items() if v <= 0]
        if bad:
            raise ValueError(f"reported counts must be positive; got {bad}")
        return self

    @property
    def total(self) -> int:
        return sum(self.reported.values()) + self.not_reported


class WindowSources(AthletrixModel):
    """Per-stream source breakdown of the samples selected for a window."""

    physiological: SourceBreakdown = Field(..., description="Sources of the selected physiological samples.")
    tracking: SourceBreakdown = Field(..., description="Sources of the selected tracking samples.")
