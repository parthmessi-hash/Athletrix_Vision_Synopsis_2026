"""Saved reviews: a result kept together with every input needed to reproduce it later.

A saved review stores the exact configuration content (rubric, index profile, substitution profile, with their SHA-256),
copies of the evidence it used (performance observations, baselines, coach-context and roster entries) and, for recorded
samples, a SHA-256 digest of what was read (the samples themselves stay in the recording). Reproducing re-runs the
calculation from the stored inputs, re-reading only the samples and checking their digest, and reports whether the
result matches. A saved review is never recalculated with the engine's current settings.
"""

from typing import Literal

from pydantic import AwareDatetime, Field

from app.models.common import AthletrixModel

ReviewKind = Literal["performance", "physiological_index", "substitution"]


class ReviewRequest(AthletrixModel):
    kind: ReviewKind
    at: AwareDatetime = Field(..., description="The review time: no observation after it is used (see each calculation's rules).")
    window_s: float | None = Field(None, gt=0, le=6 * 3600, description="performance: only observations that started within this window before `at`.")
    label: str | None = Field(None, min_length=1, max_length=120)


class SavedReview(AthletrixModel):
    id: str
    recording_id: str
    athlete_id: str
    kind: ReviewKind
    at: AwareDatetime
    created_at: AwareDatetime
    label: str | None = None
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = None
    inputs: dict
    result: dict
