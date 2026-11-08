"""The experimental substitution-review profile (see app/core/substitution.py for the rules it configures).

Every threshold, persistence count, cooldown and context policy here is a configuration choice. The engine ships none
(SUBSTITUTION_PROFILE_PATH unset means every review returns "insufficient_evidence"); a FICTIONAL example exists for
tests and demonstrations only. Nothing here is a trained model, and no value is a validated standard.
"""

from typing import Literal

from pydantic import Field, model_validator

from app.models.common import AthletrixModel

ContextPolicy = Literal["hold_for_review", "list_only"]


class PhysiologyRule(AthletrixModel):
    concern_at: float = Field(..., ge=0, le=100, description="Index at or above this counts towards a sustained elevation.")
    clear_below: float = Field(..., ge=0, le=100, description="Index below this counts towards clearing it (hysteresis: at most concern_at).")
    persistence_points: int = Field(..., ge=1, le=50, description="Consecutive review points at or above concern_at before it is a concern.")
    clear_points: int = Field(..., ge=1, le=50)

    @model_validator(mode="after")
    def _hysteresis(self) -> "PhysiologyRule":
        if self.clear_below > self.concern_at:
            raise ValueError("clear_below must not exceed concern_at")
        return self


class PerformanceRule(AthletrixModel):
    window_seconds: float = Field(..., gt=0, le=6 * 3600, description="Trailing window of performance evidence at each review point.")
    concern_below: float = Field(..., ge=0, le=100, description="Composite below this counts towards a sustained decline.")
    clear_at: float = Field(..., ge=0, le=100, description="Composite at or above this counts towards clearing it (at least concern_below).")
    persistence_points: int = Field(..., ge=1, le=50)
    clear_points: int = Field(..., ge=1, le=50)

    @model_validator(mode="after")
    def _hysteresis(self) -> "PerformanceRule":
        if self.clear_at < self.concern_below:
            raise ValueError("clear_at must not be below concern_below")
        return self


class ContextRule(AthletrixModel):
    leadership: ContextPolicy = Field(..., description="hold_for_review: recorded task-specific leadership evidence turns 'consider replacement' into 'review later' for the coach to weigh; list_only: shown as a tradeoff.")
    pressure_situation: ContextPolicy
    partnerships: ContextPolicy = Field(..., description="Applies to recorded partnerships with a partner who is on the field.")


class Routes(AthletrixModel):
    performance_only: bool = Field(False, description="Allow a review without the physiological index, but ONLY for an athlete whose roster entry states "
                                   "inputs.physiology = 'not_used' (the session plan never included a wearable for them). A sensor that was expected "
                                   "and stopped sending never takes this route: that is missing evidence.")


class SubstitutionProfile(AthletrixModel):
    format: Literal["athletrix-substitution-profile"]
    schema_version: Literal[1]
    version: str = Field(..., min_length=1, max_length=40)
    fictional: bool
    note: str | None = Field(None, max_length=500)
    step_seconds: float = Field(..., ge=10, le=1800, description="Spacing of the review points that form each history.")
    lookback_seconds: float = Field(..., gt=0, le=4 * 3600, description="How far back the histories (and the cooldown) are evaluated.")
    physiology: PhysiologyRule
    performance: PerformanceRule
    performance_decline_requires_physiology: bool = Field(..., description="A sustained performance decline leads to 'consider replacement' only with an available physiological index.")
    context: ContextRule
    routes: Routes = Field(default_factory=Routes, description="Explicit reduced-evidence routes (none by default).")
    cooldown_seconds: float = Field(..., ge=0, le=3600, description="After the output changes, an opposite keep / consider-replacement output is held back for this long.")
    review_again_after_seconds: float = Field(..., gt=0, le=3600, description="Suggested next review for 'review later'.")
