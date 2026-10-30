"""Individual-performance evidence: structured observations for the future four-area performance score.

Areas: execution, decision_making, movement_positioning, coach_strategy (following the coach's active strategy). An
observation belongs to one recording, one athlete and one observation period, with the athlete's role, its source and
optional supporting references (video frames, event instants, the coach instruction it concerns).

Event kinds in this first version:
  pass                  execution: pass attempts and completions counted over the period (attempts are the
                        opportunities; completions the outcomes), never only a percentage.
  turnover              execution: possessions (opportunities) and turnovers (possession losses) over the period.
  tactical_observation  decision_making, movement_positioning or coach_strategy: an annotated judgement whether a stated
                        instruction or expectation was followed, partially followed or not followed, with a description.
Every observation is "assessed", "unable_to_assess" (with a reason: not visible, off camera, occluded, video quality,
ambiguous, other) or "no_opportunity" (nothing to assess happened). Missing evidence is never a zero.

Sources are kept apart: "human_annotation" (a person entered it; `reference_label` marks annotations meant as reference
labels for evaluating a future automatic method) and "human_review_of_camera" (a person judged it from the recorded
video). "camera_automatic" is reserved for a future recognizer and is refused by this version, which has none: manual
entry is never presented as automatic camera recognition.

Kept out of this layer on purpose: leadership, lineup chemistry and pressure-situation contributions belong to the
coach-context records (app/models/context.py), and fatigue or physiology never enter it, so the same evidence is not
counted twice and performance is never derived from fatigue, distance alone, preset ability or a clutch bonus.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel

Area = Literal["execution", "decision_making", "movement_positioning", "coach_strategy"]
AREAS = ("execution", "decision_making", "movement_positioning", "coach_strategy")
EventKind = Literal["pass", "turnover", "tactical_observation"]
EVENT_AREAS = {"pass": ("execution",), "turnover": ("execution",), "tactical_observation": ("decision_making", "movement_positioning", "coach_strategy")}
UnableReason = Literal["not_visible", "off_camera", "occluded", "video_quality", "ambiguous", "other"]


class FrameRef(AthletrixModel):
    video: str = Field(..., min_length=1, max_length=200, description="Video file name or id (not a path).")
    frame: int = Field(..., ge=0)
    video_time_s: float | None = Field(None, ge=0)


class PerformanceObservationCreate(AthletrixModel):
    athlete_id: str = Field(..., min_length=1, max_length=128)
    role: str | None = Field(None, min_length=1, max_length=80, description="The athlete's role during the period, e.g. 'left back'; null = not recorded.")
    area: Area
    kind: EventKind
    period_start: AwareDatetime
    period_end: AwareDatetime
    assessment: Literal["assessed", "unable_to_assess", "no_opportunity"]
    unable_reason: UnableReason | None = None
    attempts: int | None = Field(None, ge=0, le=1000, description="pass: attempts (the opportunities).")
    completions: int | None = Field(None, ge=0, le=1000, description="pass: completed passes.")
    possessions: int | None = Field(None, ge=0, le=1000, description="turnover: possessions (the opportunities).")
    turnovers: int | None = Field(None, ge=0, le=1000, description="turnover: possessions lost.")
    outcome: Literal["followed", "partially_followed", "not_followed"] | None = Field(None, description="tactical_observation: the judgement.")
    instruction: str | None = Field(None, min_length=1, max_length=200, description="tactical_observation: the instruction or expectation judged, in the coach's words.")
    coach_context_id: str | None = Field(None, min_length=1, max_length=64, description="Optional coach-context entry of the same recording that holds the instruction.")
    description: str | None = Field(None, min_length=1, max_length=500)
    evidence_source: Literal["human_annotation", "human_review_of_camera", "camera_automatic"] = "human_annotation"
    reference_label: bool = Field(False, description="True when a human annotation is meant as a reference label for evaluating an automatic method.")
    annotator: str | None = Field(None, min_length=1, max_length=80)
    frames: list[FrameRef] = Field(default_factory=list, max_length=20)
    event_times: list[AwareDatetime] = Field(default_factory=list, max_length=50)
    note: str | None = Field(None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> "PerformanceObservationCreate":
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        if self.area not in EVENT_AREAS[self.kind]:
            raise ValueError(f"a {self.kind} observation belongs to {', '.join(EVENT_AREAS[self.kind])}, not {self.area}")
        if (self.assessment == "unable_to_assess") != (self.unable_reason is not None):
            raise ValueError("unable_reason is required exactly when assessment is 'unable_to_assess'")
        counts = {"pass": ("attempts", "completions"), "turnover": ("possessions", "turnovers"), "tactical_observation": ()}[self.kind]
        others = [f for f in ("attempts", "completions", "possessions", "turnovers") if f not in counts and getattr(self, f) is not None]
        if others:
            raise ValueError(f"{', '.join(others)} do not belong to a {self.kind} observation")
        if self.assessment == "assessed":
            if self.kind == "pass":
                if self.attempts is None or self.completions is None or self.completions > self.attempts or self.attempts == 0:
                    raise ValueError("an assessed pass observation needs attempts >= 1 and 0 <= completions <= attempts (no attempts is 'no_opportunity')")
            if self.kind == "turnover":
                if self.possessions is None or self.turnovers is None or self.turnovers > self.possessions or self.possessions == 0:
                    raise ValueError("an assessed turnover observation needs possessions >= 1 and 0 <= turnovers <= possessions")
            if self.kind == "tactical_observation" and (self.outcome is None or self.instruction is None):
                raise ValueError("an assessed tactical observation needs an outcome and the instruction it judges")
        elif any(getattr(self, f) is not None for f in ("attempts", "completions", "possessions", "turnovers", "outcome")):
            raise ValueError("counts and outcomes are recorded only for assessed observations")
        if self.kind != "tactical_observation" and (self.outcome is not None or self.instruction is not None):
            raise ValueError("outcome and instruction belong to tactical observations")
        if self.reference_label and self.evidence_source == "camera_automatic":
            raise ValueError("a reference label is a human annotation, not automatic output")
        if any(not (self.period_start <= t <= self.period_end) for t in self.event_times):
            raise ValueError("event_times must lie within the observation period")
        return self


class PerformanceObservation(PerformanceObservationCreate):
    id: str
    recording_id: str
    created_at: AwareDatetime
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = None


class RubricMeasure(AthletrixModel):
    """One measure of an area: how assessed evidence becomes a 0-100 area contribution, entirely from the rubric file."""

    measure: Literal["pass_completion", "possession_retention", "instruction_followed"]
    weight: float = Field(..., gt=0)
    floor: float = Field(..., ge=0, le=1, description="Rate mapped to 0.")
    ceiling: float = Field(..., ge=0, le=1, description="Rate mapped to 100.")
    partial_credit: float | None = Field(None, ge=0, le=1, description="instruction_followed: value of 'partially_followed' (required for that measure).")
    min_opportunities: int = Field(..., ge=1)

    @model_validator(mode="after")
    def _ok(self) -> "RubricMeasure":
        if self.ceiling <= self.floor:
            raise ValueError("ceiling must be above floor")
        if (self.measure == "instruction_followed") != (self.partial_credit is not None):
            raise ValueError("partial_credit is required for instruction_followed and only there")
        return self


class RubricArea(AthletrixModel):
    weight: float = Field(..., gt=0)
    measures: list[RubricMeasure] = Field(..., min_length=1)


class PerformanceRubric(AthletrixModel):
    """A versioned rubric. Weights, floors, ceilings and minimum opportunities are the team's decisions; none ship by default."""

    format: Literal["athletrix-performance-rubric"]
    version: str = Field(..., min_length=1, max_length=40)
    fictional: bool = Field(..., description="True for a test rubric with made-up values; such results are labelled fictional.")
    areas: dict[Area, RubricArea]
    note: str | None = Field(None, max_length=500)
