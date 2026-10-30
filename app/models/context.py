"""Coach-context records: time-bounded match context entered by people, kept with a recording for later review and export.

A context entry says what a coach (or another named source) stated about a match situation from a given instant:
the team's formation and tactical objective, or for one athlete their role, individual instruction, lineup
partnerships and task-specific leadership or pressure-situation evidence. Every item carries an explicit status:
"recorded" (a value is given), "unknown" (asked, but nobody knows) or "not_assessed" (nobody looked; the default).
Missing evidence therefore stays visibly missing instead of becoming a zero.

These are inputs prepared for a later substitution model; they are not scores. There are deliberately no numeric
fields: nothing here is a rating, a fixed clutch bonus, a leadership score or a personality or mental-health
description. Evidence is tied to a task and, optionally, an instant ("organised the back line at corners", not
"is a leader"). Notes are short free text in the coach's words and are never interpreted by the engine.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel

ItemStatus = Literal["recorded", "unknown", "not_assessed"]
ContextSource = Literal["head_coach", "assistant_coach", "analyst", "player_self_report", "other"]
Certainty = Literal["confirmed", "probable", "uncertain"]

EFFECTIVE_RULE = ("For a scope (the team, or one athlete), the entry in force at an instant is the one with the latest "
                  "effective_from at or before it whose effective_to, if any, is after it; at equal effective_from the "
                  "later-created entry (a correction) wins. Team and athlete entries are selected separately and never merged.")


class AssessedText(AthletrixModel):
    """One short statement with its status. A value is required exactly when the status is "recorded"."""

    status: ItemStatus = Field("not_assessed", description="recorded | unknown | not_assessed (default).")
    value: str | None = Field(None, min_length=1, max_length=200, description="The statement in the source's words; only with status recorded.")

    @model_validator(mode="after")
    def _value_matches_status(self) -> "AssessedText":
        if (self.status == "recorded") != (self.value is not None and bool(self.value.strip())):
            raise ValueError("a value is required when status is 'recorded' and not allowed otherwise")
        return self


class Partnership(AthletrixModel):
    """An observed lineup partnership with another athlete of the same recording, in the coach's words. No rating."""

    partner_athlete_id: str = Field(..., min_length=1, max_length=128)
    note: str = Field(..., min_length=1, max_length=300, description="What was observed, e.g. 'overlaps well with the left back on build-up'.")


class EvidenceItem(AthletrixModel):
    """Task-specific evidence of leadership or of a contribution in a pressure situation. A description, never a bonus."""

    kind: Literal["leadership", "pressure_situation"]
    task: str = Field(..., min_length=1, max_length=120, description="The task or situation, e.g. 'organising the back line at set pieces'.")
    description: str = Field(..., min_length=1, max_length=300, description="What was observed.")
    observed_at: AwareDatetime | None = Field(None, description="When it was observed, if known (timezone required).")


class PartnershipNotes(AthletrixModel):
    status: ItemStatus = "not_assessed"
    items: list[Partnership] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _items_match_status(self) -> "PartnershipNotes":
        if (self.status == "recorded") != bool(self.items):
            raise ValueError("partnership items are required when status is 'recorded' and not allowed otherwise")
        return self


class EvidenceNotes(AthletrixModel):
    status: ItemStatus = "not_assessed"
    items: list[EvidenceItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _items_match_status(self) -> "EvidenceNotes":
        if (self.status == "recorded") != bool(self.items):
            raise ValueError("evidence items are required when status is 'recorded' and not allowed otherwise")
        return self


class ContextCreate(AthletrixModel):
    """A context entry as entered. Team-level (athlete_id null): formation and tactical objective. Athlete-level: role,
    individual instruction, partnerships and evidence. Items that do not belong to the scope must stay unrecorded."""

    athlete_id: str | None = Field(None, min_length=1, max_length=128, description="Null for a team-level entry; otherwise an athlete of the recording's roster.")
    effective_from: AwareDatetime = Field(..., description="From when this context applies (within the recording; timezone required).")
    effective_to: AwareDatetime | None = Field(None, description="Optional end; otherwise until a later entry of the same scope takes over.")
    match_label: str | None = Field(None, min_length=1, max_length=120, description="Optional match or session label, e.g. 'U17 v Fictional FC, second half'.")
    formation: AssessedText = Field(default_factory=AssessedText, description="Team-level only, e.g. '4-3-3'.")
    tactical_instruction: AssessedText = Field(default_factory=AssessedText, description="The team's tactical objective, or this athlete's instruction.")
    role: AssessedText = Field(default_factory=AssessedText, description="Athlete-level only: the role asked of the player, e.g. 'inverted left back'.")
    partnerships: PartnershipNotes = Field(default_factory=PartnershipNotes, description="Athlete-level only.")
    evidence: EvidenceNotes = Field(default_factory=EvidenceNotes, description="Athlete-level only: task-specific leadership or pressure-situation evidence.")
    source: ContextSource = Field(..., description="Who stated this context.")
    recorded_by: str | None = Field(None, min_length=1, max_length=80, description="Optional name or initials of the person who entered it.")
    certainty: Certainty = Field(..., description="The source's own certainty about the entry as a whole.")
    note: str | None = Field(None, min_length=1, max_length=500, description="Optional short note in the source's words; never interpreted.")

    @model_validator(mode="after")
    def _scope_and_bounds(self) -> "ContextCreate":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        if self.athlete_id is None:
            wrong = [n for n in ("role", "partnerships", "evidence") if getattr(self, n).status == "recorded"]
            if wrong:
                raise ValueError(f"a team-level entry cannot record {', '.join(wrong)} (those are athlete-level)")
        elif self.formation.status == "recorded":
            raise ValueError("formation is team-level; record it in an entry without athlete_id")
        if self.athlete_id is not None and any(p.partner_athlete_id == self.athlete_id for p in self.partnerships.items):
            raise ValueError("an athlete cannot be their own partner")
        return self


class ContextEntry(ContextCreate):
    """A stored context entry. `origin` says whether it was entered on this engine or carried in an imported package."""

    id: str = Field(..., description="Generated id, e.g. ctx-20261029T120000Z-1a2b3c.")
    recording_id: str
    created_at: AwareDatetime = Field(..., description="Backend UTC time when the entry was stored on the engine where it was entered.")
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = Field(None, description="For imported entries: the id on the engine where it was entered.")


class EffectiveContext(AthletrixModel):
    """The team-level and athlete-level entries in force at one instant (either may be null: nothing recorded)."""

    recording_id: str
    at: AwareDatetime
    athlete_id: str | None
    team: ContextEntry | None
    athlete: ContextEntry | None
    rule: str = EFFECTIVE_RULE
