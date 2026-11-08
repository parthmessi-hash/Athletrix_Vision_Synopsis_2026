"""Roster state and match constraints of a recording: the minimum needed for a substitution review.

Entries are time-bounded and entered by people, like coach context (app/models/context.py): never edited in place, a
correction is a newer entry with the same effective_from, and the same selection rule applies (EFFECTIVE_RULE).

  athlete entry (athlete_id set)  where the athlete is from that instant: on_field, bench or unavailable; the role they
                                  are playing (on_field) and the roles the coach says they can play. Roles are the
                                  coach's own words, compared as exact text (case-insensitive); nothing is inferred.
  team entry (athlete_id null)    stated match constraints: substitutions remaining (null = unknown), no substitution
                                  before an instant, athletes the coach wants kept on the field and athletes not to be
                                  brought on, each with a reason.
Unrecorded means unknown: an athlete without an entry has no known status, and no constraint is assumed.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel
from app.models.context import ContextSource


class Named(AthletrixModel):
    athlete_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=200)


class InputPlan(AthletrixModel):
    """Which inputs the session plan expects for this athlete from this entry's effective time (stated by a person).

    "expected": the source is part of the session for this athlete, so its absence is missing evidence.
    "not_used": deliberately not part of the session (for example no strap issued); only then can a profile's explicit
    performance-only route apply. Null = not stated (treated like "expected" by the review). An interval is an explicit
    sampling schedule; without one an expected sample count is unknown and never invented.
    """

    physiology: Literal["expected", "not_used"] | None = None
    camera: Literal["expected", "not_used"] | None = None
    physiology_interval_s: float | None = Field(None, gt=0, le=3600, description="Stated sampling interval of the wearable stream, seconds.")
    camera_interval_s: float | None = Field(None, gt=0, le=3600, description="Stated interval of camera-derived tracking samples, seconds.")
    note: str | None = Field(None, min_length=1, max_length=200)


class RosterCreate(AthletrixModel):
    athlete_id: str | None = Field(None, min_length=1, max_length=128, description="Null for a team-level match-constraint entry.")
    effective_from: AwareDatetime
    effective_to: AwareDatetime | None = None
    status: Literal["on_field", "bench", "unavailable"] | None = Field(None, description="Athlete entries only (required there).")
    current_role: str | None = Field(None, min_length=1, max_length=80, description="Athlete entries, on_field: the role being played.")
    able_roles: list[str] = Field(default_factory=list, max_length=10, description="Athlete entries: roles the coach says the athlete can play.")
    inputs: InputPlan | None = Field(None, description="Athlete entries: the inputs the session plan expects (null = not stated).")
    substitutions_remaining: int | None = Field(None, ge=0, le=20, description="Team entries: null = unknown.")
    no_substitution_before: AwareDatetime | None = Field(None, description="Team entries: the coach's stated earliest substitution time.")
    keep_on_field: list[Named] = Field(default_factory=list, max_length=20, description="Team entries.")
    do_not_bring_on: list[Named] = Field(default_factory=list, max_length=20, description="Team entries.")
    source: ContextSource
    recorded_by: str | None = Field(None, min_length=1, max_length=80)
    note: str | None = Field(None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def _scope(self) -> "RosterCreate":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        team_fields = self.substitutions_remaining is not None or self.no_substitution_before is not None or self.keep_on_field or self.do_not_bring_on
        if self.athlete_id is None:
            if self.status or self.current_role or self.able_roles or self.inputs:
                raise ValueError("status, roles and inputs belong to athlete entries")
        else:
            if team_fields:
                raise ValueError("match constraints belong to a team-level entry (no athlete_id)")
            if self.status is None:
                raise ValueError("an athlete entry needs a status")
            if self.current_role is not None and self.status != "on_field":
                raise ValueError("current_role is only recorded for an athlete on the field")
        if any(not r.strip() for r in self.able_roles):
            raise ValueError("roles must not be blank")
        return self


class RosterEntry(RosterCreate):
    id: str
    recording_id: str
    created_at: AwareDatetime
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = None
