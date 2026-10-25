from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel
from app.models.physiological import PhysiologicalSample
from app.models.sources import WindowSources
from app.models.tracking import TrackingSample


class TrackingRules(AthletrixModel):
    """The engine's applied tracking rule, echoed with the samples it applies to.

    `min_confidence` is the same `DISTANCE_MIN_CONFIDENCE` setting the distance
    and high-speed estimates use: a tracking sample whose `tracking_confidence`
    is below it is treated as not usable. Comparing a sample's confidence with
    this value says nothing about the accuracy of the sample's speed or
    position; it only reflects the confidence the tracking source reported.
    An adjustable engineering default, not a validated standard.
    """

    min_confidence: float = Field(..., ge=0, le=1, description="Minimum tracking_confidence for a sample to count as usable.")


class AthleteWindow(AthletrixModel):
    """Samples recorded for one athlete within a recent time window.

    This groups samples purely by their recorded timestamps. It does not
    correct for clock differences between devices, and it does not account
    for the lag between physical effort and physiological response. Both
    lists are limited by the per-stream history cap, so a long window may
    return fewer samples than the window could hold.

    `tracking_rules` carries the confidence rule in force when the samples
    were selected, so a consumer can classify the tracking samples from this
    response alone without a second request. `sources` counts, per stream,
    how many of exactly these samples reported each source and how many
    reported none; it is derived from the sample lists when not given, so it
    always adds up to them.
    """

    athlete_id: str = Field(..., description="Athlete the window belongs to.")
    window_start: AwareDatetime = Field(..., description="Inclusive start of the window (UTC).")
    window_end: AwareDatetime = Field(..., description="Inclusive end of the window: backend time at the request (UTC).")
    physiological_samples: list[PhysiologicalSample] = Field(..., description="Oldest to newest.")
    tracking_samples: list[TrackingSample] = Field(..., description="Oldest to newest.")
    tracking_rules: TrackingRules = Field(..., description="Applied tracking rule (the engine's configured minimum confidence).")
    sources: WindowSources | None = Field(None, description="Per-stream source breakdown of the samples above; derived from them when omitted.")

    @model_validator(mode="after")
    def _derive_sources(self) -> "AthleteWindow":
        if self.sources is None:
            from app.core.sources import source_breakdown  # local import: app.core.sources depends on app.models

            self.sources = WindowSources(
                physiological=source_breakdown(self.physiological_samples),
                tracking=source_breakdown(self.tracking_samples),
            )
        return self
