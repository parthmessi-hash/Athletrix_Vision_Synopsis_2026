"""Coach / operator responses to experimental substitution reviews, and recorded lineup changes.

A response is feedback about what a person did with a recommendation; it is stored apart from the recommendation, which
it references through a saved review (app/models/review.py) so the recommendation, its profiles and its evidence stay
exactly as they were shown. Nothing here retrains or adjusts anything, and an override is not evidence that the
recommendation was right or wrong.

  acknowledged   seen; changes nothing
  keep           the coach keeps the athlete on
  defer          review later (optionally at a stated time)
  dismiss        not followed, with an optional reason
  lineup_change  an actual substitution: explicit outgoing and incoming athletes and an effective time. It writes roster
                 entries (outgoing: unavailable, "substituted off"; incoming: on_field in the stated role) and, only when
                 the number of substitutions remaining is recorded, a team entry with one fewer. Unknown stays unknown.

`client_token` makes a save idempotent: a retry or double click with the same token returns the stored response
instead of saving a second one. `simulated` marks decisions made by a demo script, not by a person.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel


class LineupChange(AthletrixModel):
    outgoing_athlete_id: str = Field(..., min_length=1, max_length=128)
    incoming_athlete_id: str = Field(..., min_length=1, max_length=128)
    effective_from: AwareDatetime
    incoming_role: str | None = Field(None, min_length=1, max_length=80, description="Default: the outgoing athlete's current role.")
    accept_role_mismatch: bool = Field(False, description="Record the change although the incoming athlete's recorded roles do not include the role.")


class ResponseCreate(AthletrixModel):
    athlete_id: str = Field(..., min_length=1, max_length=128, description="The athlete the review was about (for a lineup change: the outgoing athlete).")
    kind: Literal["acknowledged", "keep", "defer", "dismiss", "lineup_change"]
    client_token: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="Unique per intended action; a retry with the same token returns the stored response.")
    saved_review_id: str | None = Field(None, min_length=1, max_length=64, description="The saved review the response is about (required except for a lineup change).")
    reason: str | None = Field(None, min_length=1, max_length=300)
    review_again_at: AwareDatetime | None = Field(None, description="defer only.")
    lineup: LineupChange | None = None
    recorded_by: str | None = Field(None, min_length=1, max_length=80)
    simulated: bool = Field(False, description="True when a demo script, not a person, made this decision.")

    @model_validator(mode="after")
    def _shape(self) -> "ResponseCreate":
        if (self.kind == "lineup_change") != (self.lineup is not None):
            raise ValueError("lineup is required for a lineup_change and only there")
        if self.kind != "lineup_change" and self.saved_review_id is None:
            raise ValueError("a response to a recommendation names the saved review it responds to (save the review first)")
        if self.review_again_at is not None and self.kind != "defer":
            raise ValueError("review_again_at belongs to defer")
        if self.lineup is not None and self.lineup.outgoing_athlete_id != self.athlete_id:
            raise ValueError("athlete_id must be the outgoing athlete of the lineup change")
        if self.lineup is not None and self.lineup.incoming_athlete_id == self.lineup.outgoing_athlete_id:
            raise ValueError("incoming and outgoing athletes must differ")
        return self


class RecommendationRef(AthletrixModel):
    """Copied from the saved review by the engine (not from the client)."""

    saved_review_id: str
    outcome: str | None
    review_at: AwareDatetime
    knowledge: str
    profiles: dict | None


class CoachResponse(ResponseCreate):
    id: str
    recording_id: str
    created_at: AwareDatetime
    recommendation: RecommendationRef | None = None
    roster_entry_ids: list[str] = Field(default_factory=list, description="Roster entries this lineup change wrote.")
    warnings: list[str] = Field(default_factory=list)
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = None
    note: str = "A response is feedback about what a person did; it does not show the recommendation was right or wrong, and nothing is retrained from it."
