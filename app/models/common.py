"""Shared enums and base configuration for all Athletrix models."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AthletrixModel(BaseModel):
    """Base class: unknown fields are rejected so typos surface as 422s."""

    model_config = ConfigDict(extra="forbid")


class SampleSource(StrEnum):
    """Generic category of where a sample came from (not a vendor name)."""

    WEARABLE = "wearable"
    CAMERA = "camera"
    MANUAL = "manual"
    SIMULATED = "simulated"


class Posture(StrEnum):
    """Coarse body posture classification."""

    STANDING = "standing"
    SITTING = "sitting"
    LYING = "lying"
    WALKING = "walking"
    RUNNING = "running"
    UNKNOWN = "unknown"
