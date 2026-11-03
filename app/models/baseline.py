"""Athlete physiological baselines and the experimental physiological index profile.

BASELINE. A persisted, versioned description of one athlete's reference physiology: which recording and time ranges it
was taken from, under what stated condition (seated rest, warm-up, ...), which signals, the inclusion requirements
(minimum signal quality, source, minimum samples) and, per signal, the sample counts, exclusions by reason, descriptive
statistics and the devices and sources that produced the values. It is calculated once, when created, from the stored
recording, and never recalculated; a different definition is a new baseline (the version increases per athlete).

Review status is a stated decision by a person: "pending_review" (the default), "accepted" or "rejected", with who
decided, when and why. Every decision is kept. Acceptance means someone looked at it and chose to use it; it is not a
scientific validation and says nothing about whether the baseline represents the athlete.

INDEX PROFILE. The configuration of the experimental 0-100 physiological index (see app/core/physiology_index.py):
signals, transformations and directions, bounds, weights, the rolling window, coverage, staleness and baseline
requirements. The engine ships none (PHYSIOLOGICAL_INDEX_PROFILE_PATH unset means the index is unavailable); a FICTIONAL
example profile exists for tests and demonstrations only. The index is an experimental physiological / fatigue index: it
does not confirm fatigue, injury, safety or readiness.
"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.models.common import AthletrixModel, SampleSource

Signal = Literal["heart_rate_bpm", "breathing_rate_bpm", "hrv_rmssd_ms", "hrv_sdnn_ms", "skin_temperature_c", "spo2_percent"]
Condition = Literal["seated_rest", "standing_rest", "lying_rest", "warm_up", "light_activity", "match_play", "other"]
SIGNAL_DEFINITIONS = {
    "heart_rate_bpm": "heart rate, beats per minute",
    "breathing_rate_bpm": "breathing rate, breaths per minute",
    "hrv_rmssd_ms": "HRV as RMSSD in milliseconds (a calculated statistic, not beat intervals; not comparable with SDNN)",
    "hrv_sdnn_ms": "HRV as SDNN in milliseconds (a calculated statistic, not beat intervals; not comparable with RMSSD)",
    "skin_temperature_c": "measured skin temperature, degrees Celsius (never an estimated core temperature)",
    "spo2_percent": "blood oxygen saturation, percent",
}


class TimeRange(AthletrixModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("a time range must end after it starts")
        return self


class BaselineCreate(AthletrixModel):
    source_recording_id: str = Field(..., min_length=1, max_length=64, description="A stopped recording whose roster includes the athlete.")
    ranges: list[TimeRange] = Field(..., min_length=1, max_length=10, description="Non-overlapping ranges within the recording (start and end inclusive).")
    condition: Condition = Field(..., description="The stated reference or activity condition during the ranges.")
    condition_note: str | None = Field(None, min_length=1, max_length=200, description="In the observer's words, e.g. 'seated on the bench before warm-up'.")
    signals: list[Signal] = Field(..., min_length=1, max_length=6)
    min_signal_quality: float | None = Field(None, ge=0, le=1, description="Samples below it, or without a reported quality, are excluded; null = no quality requirement.")
    required_source: SampleSource | None = Field(None, description="Only samples of this source category; null = any.")
    min_samples: int = Field(..., ge=2, le=100000, description="Per signal: fewer included values make that signal insufficient.")
    label: str | None = Field(None, min_length=1, max_length=120)
    created_by: str | None = Field(None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def _distinct(self) -> "BaselineCreate":
        if len(set(self.signals)) != len(self.signals):
            raise ValueError("signals must be distinct")
        rs = sorted(self.ranges, key=lambda r: r.start)
        if any(b.start <= a.end for a, b in zip(rs, rs[1:])):
            raise ValueError("ranges must not overlap")
        return self


class SignalStats(AthletrixModel):
    signal: Signal
    definition: str
    included: int = Field(..., ge=0)
    excluded: dict[str, int] = Field(default_factory=dict, description="reason -> samples left out (missing value, quality not reported, low quality, other source).")
    mean: float | None = None
    sd: float | None = Field(None, description="Sample standard deviation (n - 1); null with fewer than two values.")
    median: float | None = None
    min: float | None = None
    max: float | None = None
    first_at: AwareDatetime | None = None
    last_at: AwareDatetime | None = None
    device_ids: list[str] = Field(default_factory=list)
    sources: dict[str, int] = Field(default_factory=dict)
    sufficient: bool
    insufficient_reason: str | None = None


class BaselineDecision(AthletrixModel):
    decision: Literal["accepted", "rejected"]
    decided_by: str = Field(..., min_length=1, max_length=80)
    note: str = Field(..., min_length=1, max_length=300, description="Why, in the reviewer's words.")


class RecordedDecision(BaselineDecision):
    decided_at: AwareDatetime


class Baseline(BaselineCreate):
    id: str
    athlete_id: str
    version: int = Field(..., ge=1, description="Per athlete, increasing with each new baseline.")
    created_at: AwareDatetime
    data_start: AwareDatetime = Field(..., description="Earliest instant the ranges cover.")
    data_end: AwareDatetime = Field(..., description="Latest instant the ranges cover: a review before it uses later data (retrospective).")
    stats: dict[Signal, SignalStats]
    status: Literal["pending_review", "accepted", "rejected"] = "pending_review"
    decisions: list[RecordedDecision] = Field(default_factory=list)
    origin: Literal["this_engine", "source_engine"] = "this_engine"
    source_entry_id: str | None = None
    status_note: str = "A review status is a person's recorded decision, not a scientific validation of the baseline."


class IndexSignal(AthletrixModel):
    signal: Signal
    transform: Literal["z_score", "percent_change"] = Field(..., description="z_score: (window mean - baseline mean) / baseline SD; percent_change: 100 x (window mean - baseline mean) / baseline mean.")
    direction: Literal["higher_is_more_strain", "lower_is_more_strain"] = Field(..., description="lower_is_more_strain negates the transformed value before scoring.")
    zero_at: float = Field(..., description="Directed transformed value that scores 0.")
    hundred_at: float = Field(..., description="Directed transformed value that scores 100; values beyond either bound are clamped.")
    weight: float = Field(..., gt=0)
    min_samples_in_window: int = Field(..., ge=1)

    @model_validator(mode="after")
    def _bounds(self) -> "IndexSignal":
        if self.hundred_at == self.zero_at:
            raise ValueError("hundred_at must differ from zero_at")
        return self


class BaselineRequirements(AthletrixModel):
    require_status: Literal["accepted", "pending_or_accepted"] = Field(..., description="Which review states a usable baseline may have (rejected never).")
    conditions: list[Condition] | None = Field(None, description="Allowed baseline conditions; null = any.")
    min_samples: int = Field(..., ge=2, description="Per signal, in the baseline.")
    same_device_required: bool = Field(..., description="Window samples from a device that did not contribute to the baseline are excluded.")
    allow_retrospective: bool = Field(..., description="Whether a baseline that uses data after the review time may be used (the result is then labelled retrospective).")


class IndexProfile(AthletrixModel):
    format: Literal["athletrix-physiological-index-profile"]
    schema_version: Literal[1]
    version: str = Field(..., min_length=1, max_length=40)
    fictional: bool = Field(..., description="True for made-up values; every result is then labelled fictional.")
    note: str | None = Field(None, max_length=500)
    window_seconds: float = Field(..., gt=0, le=3600, description="Rolling window (at - window, at].")
    expected_interval_seconds: float = Field(..., gt=0, le=600, description="Nominal sample interval, for input coverage.")
    min_input_coverage: float = Field(..., ge=0, le=1, description="Per signal: included values x expected interval / window must reach this.")
    staleness_seconds: float = Field(..., gt=0, description="Per signal: the newest included value must be at most this old at the review time.")
    min_weight_coverage: float = Field(..., gt=0, le=1, description="Available signals' share of the total weight needed for an index.")
    baseline: BaselineRequirements
    signals: list[IndexSignal] = Field(..., min_length=1, max_length=6)

    @model_validator(mode="after")
    def _distinct(self) -> "IndexProfile":
        if len({s.signal for s in self.signals}) != len(self.signals):
            raise ValueError("each signal may appear once")
        return self
