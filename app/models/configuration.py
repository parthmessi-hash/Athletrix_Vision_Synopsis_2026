"""The engine configuration captured when a recording starts (snapshot version 1), and operator-supplied adapter notes.

A snapshot records the settings that shaped acquisition and storage at the start: movement calculation rules,
rolling retention caps, the timestamp skew allowance, the fixed effort-review limits, the application version and,
only when it was supplied at startup, a code revision. It never holds secrets, paths, hostnames, usernames, CORS
origins or arbitrary environment variables. `operator_supplied` is information the operator typed in when starting
the recording (for example the adapter's own configuration); the engine stores it as given and verifies none of it.
"""

import math
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator

from app.models.common import AthletrixModel

SNAPSHOT_VERSION = 1
MAX_NOTE_CHARS = 500
MAX_METADATA_KEYS = 32
MAX_METADATA_KEY_CHARS = 64
MAX_METADATA_VALUE_CHARS = 200

MetadataValue = str | int | float | bool | None


class AdapterConfiguration(AthletrixModel):
    """Optional, bounded description of the input adapter supplied by the operator at recording start. Not verified."""

    note: str | None = Field(None, max_length=MAX_NOTE_CHARS, description="Free text, e.g. 'wearable bridge v0.3, 1 Hz, camera calibration 2026-10-27'.")
    metadata: dict[str, MetadataValue] = Field(default_factory=dict, description=f"Flat key/value pairs (at most {MAX_METADATA_KEYS}); strings up to {MAX_METADATA_VALUE_CHARS} characters.")

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return v.strip() or None if v is not None else None

    @field_validator("metadata")
    @classmethod
    def _metadata_bounded(cls, v: dict) -> dict:
        if len(v) > MAX_METADATA_KEYS:
            raise ValueError(f"at most {MAX_METADATA_KEYS} metadata keys")
        for k, value in v.items():
            if not k.strip() or len(k) > MAX_METADATA_KEY_CHARS:
                raise ValueError(f"metadata keys must be 1 to {MAX_METADATA_KEY_CHARS} non-blank characters")
            if isinstance(value, str) and len(value) > MAX_METADATA_VALUE_CHARS:
                raise ValueError(f"metadata value for {k!r} is longer than {MAX_METADATA_VALUE_CHARS} characters")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"metadata value for {k!r} must be a finite number")
        return v


class OperatorSupplied(AdapterConfiguration):
    verified_by_engine: Literal[False] = Field(False, description="Always false: supplied by the operator, stored as given, never checked.")


class EngineIdentity(AthletrixModel):
    application: str
    version: str = Field(..., description="APP_VERSION as configured for this engine.")
    code_revision: str | None = Field(None, description="CODE_REVISION supplied at startup, as given (not checked against any repository); null = unknown.")
    package_format_version: int = Field(..., description="Portable package format this engine writes.")


class MovementSettings(AthletrixModel):
    distance_max_gap_seconds: float
    distance_min_confidence: float
    high_speed_threshold_mps: float


class RetentionSettings(AthletrixModel):
    physiological_retention_samples: int
    tracking_retention_samples: int


class TimingSettings(AthletrixModel):
    max_future_timestamp_skew_seconds: float = Field(..., description="How far ahead of backend time a sample timestamp may be when accepted live.")


class EffortLimits(AthletrixModel):
    min_effort_seconds: float
    max_effort_seconds: float
    before_seconds: float
    after_seconds: float


class RecordingConfiguration(AthletrixModel):
    """Snapshot version 1, captured in the transaction that started the recording."""

    snapshot_version: Literal[1] = SNAPSHOT_VERSION
    captured_at: AwareDatetime = Field(..., description="Backend time of the recording start (equals the recording's started_at).")
    engine: EngineIdentity
    movement: MovementSettings
    retention: RetentionSettings
    timing: TimingSettings
    effort_review: EffortLimits
    operator_supplied: OperatorSupplied | None = Field(None, description="Adapter information typed in by the operator at start; null if none was given.")


class RecordingConfigurationStatus(AthletrixModel):
    """Whether a start-time snapshot exists for a recording, where it was captured, and the snapshot itself."""

    status: Literal["captured", "unavailable"]
    origin: Literal["this_engine", "source_engine"] | None = Field(None, description="this_engine: captured here at start; source_engine: carried in an imported package from the engine where the recording was made.")
    snapshot: RecordingConfiguration | None = None
    note: str
