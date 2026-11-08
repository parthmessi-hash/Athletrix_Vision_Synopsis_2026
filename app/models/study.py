"""Study records of the science-fair fatigue pilot: one append-only log per recording (`recording_study`).

KINDS (operator entries unless noted)
  session        the study session: label (e.g. "day1"), protocol, fictional flag, calibration and clock references,
                 planned rounds; the engine adds the frozen fatigue profile (its exact text and SHA-256) on creation
  wearer         the active wearable assignment from `at`: participant athlete, pseudonymous participant code, device,
                 the participant's OWN baseline and the maximum heart rate the profile may use, with its origin. The next
                 wearer entry (or an `end` wearer entry) closes it. Readings outside a participant's own assignment, or
                 from another device, are never used for that participant.
  round_start / round_end    drill round boundaries (any number of rounds); round_end with invalid=true (and the
                 reason in notes) leaves the round out of the evaluation
  sprint_start / sprint_end  a planned timed test sprint of a round. Marking sprint_start makes the engine save a
                 PREDICTION (kind "prediction", written by the engine only) with the scores as available at that cutoff, in
                 the same transaction as the marker. If the stated sprint time (`at`) is more than
                 PROSPECTIVE_TOLERANCE_S before the marker reached the engine, the prediction is labelled RETROSPECTIVE
                 (backdated marker): calculated with what was entered by the stated time, but not captured before the
                 sprint, so the evaluation never counts it as a pre-sprint prediction.
  sprint_result  the independently timed result, entered later (time in seconds, or null with a missing reason)
  self_rating    optional self-rating (0-10) or null with a missing reason, plus notes; never a score input
  note           free text
Entries are never edited. A correction is a new entry with `supersedes` and `correction_reason`; the older one stays
as history. Each entry has a client_token: the same token again returns the stored entry (retries are safe).
TIMES: `at` is the event time on the engine's clock (default: when the engine received the entry); `created_at` is the
receipt time. Sample timestamps, sample receipt times and any clock mapping applied by an adapter stay separate (the
session's clock_references say which mapping was used; samples keep their own receipts).
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel

StudyKind = Literal["session", "wearer", "round_start", "round_end", "sprint_start", "sprint_end", "sprint_result", "self_rating", "note"]
MissingReason = Literal["not_collected", "declined", "not_recorded", "invalid_attempt", "equipment_failure", "other"]
PROSPECTIVE_TOLERANCE_S = 2.0
ROUND_KINDS = ("round_start", "round_end", "sprint_start", "sprint_end", "sprint_result", "self_rating")


class ClockReference(AthletrixModel):
    stream: Literal["camera", "wearable"]
    basis: Literal["capture_time_mapping", "operator_estimate", "replay_anchor", "unknown"]
    uncertainty_s: float | None = Field(None, ge=0)
    alignment_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    note: str | None = Field(None, max_length=200)


class StudyEntryCreate(AthletrixModel):
    kind: StudyKind
    client_token: str = Field(..., min_length=8, max_length=80)
    at: AwareDatetime | None = Field(None, description="Event time on the engine clock; default = receipt time.")
    athlete_id: str | None = Field(None, min_length=1, max_length=128, description="The participant (engine athlete id); required except for session and note.")
    round: int | None = Field(None, ge=1, le=100)
    # session
    session_label: str | None = Field(None, min_length=1, max_length=40)
    protocol: Literal["controlled_study", "club_demo", "rehearsal"] | None = None
    fictional: bool | None = None
    calibration_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    clock_references: list[ClockReference] = Field(default_factory=list, max_length=4)
    planned_rounds: int | None = Field(None, ge=1, le=100)
    # wearer
    participant_code: str | None = Field(None, pattern=r"^[A-Z]{1,3}[0-9]{1,3}$", description="Pseudonymous code such as P1; never a name.")
    device_id: str | None = Field(None, min_length=1, max_length=128)
    baseline_id: str | None = Field(None, min_length=1, max_length=64)
    hr_max_bpm: float | None = Field(None, gt=100, lt=240)
    hr_max_origin: str | None = Field(None, min_length=1, max_length=120, description="Where the maximum heart rate came from, e.g. 'estimated: 208 - 0.7 x age' or 'measured test'.")
    end: bool = Field(False, description="wearer: true ends the athlete's assignment at `at` without a new one.")
    invalid: bool = Field(False, description="round_end: the operator marks the round invalid (the reason goes in notes); it is left out of the evaluation.")
    # sprint_result / self_rating
    time_s: float | None = Field(None, gt=0, lt=60)
    method: Literal["timing_gates", "stopwatch", "video_frames", "other"] | None = None
    value: float | None = Field(None, ge=0, le=10)
    scale: str | None = Field(None, max_length=80)
    missing_reason: MissingReason | None = None
    recorded_by: str | None = Field(None, min_length=1, max_length=80)
    notes: str | None = Field(None, max_length=500)
    supersedes: str | None = Field(None, min_length=1, max_length=64)
    correction_reason: str | None = Field(None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def _shape(self) -> "StudyEntryCreate":
        k = self.kind
        if (self.supersedes is None) != (self.correction_reason is None):
            raise ValueError("a correction needs both supersedes and correction_reason")
        if k not in ("session", "note") and not self.athlete_id:
            raise ValueError(f"a {k} entry needs athlete_id (the participant)")
        if k in ROUND_KINDS and self.round is None:
            raise ValueError(f"a {k} entry needs the round number")
        if k == "session" and (self.session_label is None or self.protocol is None or self.fictional is None):
            raise ValueError("a session entry needs session_label, protocol and fictional")
        if k == "wearer" and not self.end and (self.participant_code is None or self.device_id is None or self.baseline_id is None):
            raise ValueError("a wearer entry needs participant_code, device_id and baseline_id (the participant's own baseline)")
        if (self.hr_max_bpm is None) != (self.hr_max_origin is None):
            raise ValueError("hr_max_bpm needs its origin (hr_max_origin), and the reverse")
        if k == "sprint_result" and (self.time_s is None) == (self.missing_reason is None):
            raise ValueError("a sprint result has either time_s or a missing_reason")
        if k == "sprint_result" and self.time_s is not None and self.method is None:
            raise ValueError("a timed sprint result needs the timing method")
        if k == "self_rating" and (self.value is None) == (self.missing_reason is None):
            raise ValueError("a self-rating has either a value or a missing_reason (an explicit missing value)")
        if self.invalid and (k != "round_end" or not self.notes):
            raise ValueError("only a round_end can mark a round invalid, and it needs the reason in notes")
        if k == "note" and not self.notes:
            raise ValueError("a note needs notes")
        return self


class StudyEntry(StudyEntryCreate):
    kind: Literal["session", "wearer", "round_start", "round_end", "sprint_start", "sprint_end", "sprint_result", "self_rating", "note", "prediction"]  # type: ignore[assignment]
    id: str
    recording_id: str
    at: AwareDatetime  # type: ignore[assignment]
    created_at: AwareDatetime
    profile: dict | None = Field(None, description="session: the frozen fatigue profile {text, sha256, version, fictional}; null = none configured.")
    prediction: dict | None = Field(None, description="prediction: the saved scores and evidence at the cutoff (engine-written).")
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = None
