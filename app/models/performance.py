"""Individual-performance evidence: structured observations for the future four-area performance score.

Areas: execution, decision_making, movement_positioning, coach_strategy (following the coach's active strategy). An
observation belongs to one recording, one athlete and one observation period, with the athlete's role, its source and
optional supporting references (video frames, event instants, the coach instruction it concerns).

Each area has its own kind of evidence, so one judgement never stands in for another:
  pass                  execution: pass attempts and completions counted over the period (attempts are the
                        opportunities; completions the outcomes), never only a percentage.
  turnover              execution: possessions (opportunities) and turnovers (possession losses) over the period.
  decision              decision_making: decision moments (opportunities) in the period, each judged against a stated
                        criterion as good, acceptable or poor (`criterion`, e.g. "chose the option that broke the press").
                        A completed pass is not a good decision by itself: pass completion is execution evidence only.
  positioning           movement_positioning: moments checked (opportunities), each judged good, acceptable or poor
                        against a stated positional expectation (`criterion`, e.g. "kept the back-line height").
  tactical_observation  coach_strategy: whether a stated coach instruction was followed, partially followed or not
                        followed. Version 1 of this layer also accepted it for decision_making and movement_positioning;
                        such stored or imported observations stay readable and listed but are not scored ("legacy"), and
                        new ones are refused.
Every observation is "assessed", "unable_to_assess" (with a reason: not visible, off camera, occluded, video quality,
ambiguous, other) or "no_opportunity" (nothing to assess happened). Missing evidence is never a zero.

CORRECTIONS. Observations are never edited in place. A correction is a new observation with `supersedes` (the id of the
observation it replaces, same recording and athlete) and a `correction_reason`; the superseded one stays stored as audit
history and no longer counts, so the two versions are never counted together. Only the latest version of a chain can be
corrected again.

OVERLAPS (inclusion rule). Two counted observations of the same athlete and kind whose periods overlap may count the same
opportunities twice. Both are then left out of the measure and reported as a potential duplicate, unless one lists the
other in `distinct_from` (the annotator states they count different opportunities) or one supersedes the other.

IDENTITY. `identity` says whether the annotator was sure which player the evidence belongs to ("confirmed" or
"uncertain"; omitted = not stated, e.g. live observation of a known player). Uncertain-identity observations are listed
but never scored.

Kept out of this layer on purpose: leadership, lineup chemistry and pressure-situation contributions belong to the
coach-context records (app/models/context.py), and fatigue or physiology never enter it, so the same evidence is not
counted twice and performance is never derived from fatigue, distance alone, preset ability or a clutch bonus.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel

Area = Literal["execution", "decision_making", "movement_positioning", "coach_strategy"]
AREAS = ("execution", "decision_making", "movement_positioning", "coach_strategy")
EventKind = Literal["pass", "turnover", "decision", "positioning", "tactical_observation"]
EVENT_AREAS = {"pass": ("execution",), "turnover": ("execution",), "decision": ("decision_making",), "positioning": ("movement_positioning",),
               "tactical_observation": ("decision_making", "movement_positioning", "coach_strategy")}   # the first two: legacy (version 1) only
NEW_EVENT_AREAS = {**EVENT_AREAS, "tactical_observation": ("coach_strategy",)}
COUNT_FIELDS = {"pass": ("attempts", "completions"), "turnover": ("possessions", "turnovers"), "decision": ("opportunities", "rated_good", "rated_acceptable", "rated_poor"),
                "positioning": ("opportunities", "rated_good", "rated_acceptable", "rated_poor"), "tactical_observation": ()}
ALL_COUNTS = ("attempts", "completions", "possessions", "turnovers", "opportunities", "rated_good", "rated_acceptable", "rated_poor")


def is_legacy(kind: str, area: str) -> bool:
    """A version-1 tactical observation filed under decision_making or movement_positioning (readable, never scored)."""
    return kind == "tactical_observation" and area != "coach_strategy"


def _unset(v) -> bool:
    return v is None or v == []
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
    opportunities: int | None = Field(None, ge=0, le=1000, exclude_if=_unset, description="decision / positioning: moments judged (the opportunities).")
    rated_good: int | None = Field(None, ge=0, le=1000, exclude_if=_unset)
    rated_acceptable: int | None = Field(None, ge=0, le=1000, exclude_if=_unset)
    rated_poor: int | None = Field(None, ge=0, le=1000, exclude_if=_unset)
    criterion: str | None = Field(None, min_length=1, max_length=200, exclude_if=_unset, description="decision / positioning: what each moment was judged against.")
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
    identity: Literal["confirmed", "uncertain"] | None = Field(None, exclude_if=_unset, description="Whether the annotator was sure which player this is; uncertain = listed, never scored.")
    supersedes: str | None = Field(None, min_length=1, max_length=64, exclude_if=_unset, description="Id of the observation this one corrects (same recording and athlete).")
    correction_reason: str | None = Field(None, min_length=1, max_length=300, exclude_if=_unset)
    distinct_from: list[str] = Field(default_factory=list, max_length=20, exclude_if=_unset, description="Overlapping observations this one does NOT double count (annotator's statement).")

    @model_validator(mode="after")
    def _consistent(self) -> "PerformanceObservationCreate":
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        if self.area not in EVENT_AREAS[self.kind]:
            raise ValueError(f"a {self.kind} observation belongs to {', '.join(EVENT_AREAS[self.kind])}, not {self.area}")
        if (self.assessment == "unable_to_assess") != (self.unable_reason is not None):
            raise ValueError("unable_reason is required exactly when assessment is 'unable_to_assess'")
        counts = COUNT_FIELDS[self.kind]
        others = [f for f in ALL_COUNTS if f not in counts and getattr(self, f) is not None]
        if others:
            raise ValueError(f"{', '.join(others)} do not belong to a {self.kind} observation")
        if self.assessment == "assessed":
            if self.kind == "pass":
                if self.attempts is None or self.completions is None or self.completions > self.attempts or self.attempts == 0:
                    raise ValueError("an assessed pass observation needs attempts >= 1 and 0 <= completions <= attempts (no attempts is 'no_opportunity')")
            if self.kind == "turnover":
                if self.possessions is None or self.turnovers is None or self.turnovers > self.possessions or self.possessions == 0:
                    raise ValueError("an assessed turnover observation needs possessions >= 1 and 0 <= turnovers <= possessions")
            if self.kind in ("decision", "positioning"):
                parts = (self.rated_good, self.rated_acceptable, self.rated_poor)
                if self.opportunities is None or not self.opportunities or any(p is None for p in parts) or sum(parts) != self.opportunities:
                    raise ValueError(f"an assessed {self.kind} observation needs opportunities >= 1 and rated_good + rated_acceptable + rated_poor = opportunities")
                if self.criterion is None:
                    raise ValueError(f"an assessed {self.kind} observation needs the criterion each moment was judged against")
            if self.kind == "tactical_observation" and (self.outcome is None or self.instruction is None):
                raise ValueError("an assessed tactical observation needs an outcome and the instruction it judges")
        elif any(getattr(self, f) is not None for f in (*ALL_COUNTS, "outcome")):
            raise ValueError("counts and outcomes are recorded only for assessed observations")
        if self.kind != "tactical_observation" and (self.outcome is not None or self.instruction is not None):
            raise ValueError("outcome and instruction belong to tactical observations")
        if self.kind not in ("decision", "positioning") and self.criterion is not None:
            raise ValueError("criterion belongs to decision and positioning observations")
        if (self.supersedes is None) != (self.correction_reason is None):
            raise ValueError("a correction needs both supersedes and correction_reason")
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


JUDGED_MEASURES = ("decision_quality", "positioning_quality", "instruction_followed")


class RubricMeasure(AthletrixModel):
    """One measure of an area: how assessed evidence becomes a 0-100 area contribution, entirely from the rubric file."""

    measure: Literal["pass_completion", "possession_retention", "decision_quality", "positioning_quality", "instruction_followed"]
    weight: float = Field(..., gt=0)
    floor: float = Field(..., ge=0, le=1, description="Rate mapped to 0.")
    ceiling: float = Field(..., ge=0, le=1, description="Rate mapped to 100.")
    partial_credit: float | None = Field(None, ge=0, le=1, description="decision_quality / positioning_quality: value of 'acceptable'; instruction_followed: value of 'partially_followed' (required for these measures).")
    min_opportunities: int = Field(..., ge=1)

    @model_validator(mode="after")
    def _ok(self) -> "RubricMeasure":
        if self.ceiling <= self.floor:
            raise ValueError("ceiling must be above floor")
        if (self.measure in JUDGED_MEASURES) != (self.partial_credit is not None):
            raise ValueError(f"partial_credit is required for {', '.join(JUDGED_MEASURES)} and only there")
        return self


class RubricArea(AthletrixModel):
    weight: float = Field(..., gt=0)
    measures: list[RubricMeasure] = Field(..., min_length=1)


class PerformanceRubric(AthletrixModel):
    """A versioned rubric. Weights, floors, ceilings and minimum opportunities are the team's decisions; none ship by default."""

    format: Literal["athletrix-performance-rubric"]
    schema_version: Literal[2] = Field(..., description="2: one evidence kind per area (version-1 rubrics scored decisions and positioning by instruction_followed and are refused).")
    version: str = Field(..., min_length=1, max_length=40)
    fictional: bool = Field(..., description="True for a test rubric with made-up values; such results are labelled fictional.")
    areas: dict[Area, RubricArea]
    note: str | None = Field(None, max_length=500)
