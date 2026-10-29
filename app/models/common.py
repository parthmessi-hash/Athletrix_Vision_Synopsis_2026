"""Shared enums and base configuration for all Athletrix models."""

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


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


class SampleReceipt(AthletrixModel):
    """When an engine received a sample live: receipt metadata, never the sample's identity or ordering key.

    `received_at` is the receiving engine's UTC wall clock at request receipt; it is on a different clock from the
    sample's own `timestamp` (the sender's), so their difference mixes delivery delay with any clock offset.
    """

    received_at: AwareDatetime = Field(..., description="Receiving engine's UTC wall clock when the request reached it.")
    origin: Literal["this_engine", "source_engine"] = Field(..., description="this_engine: received live here; source_engine: received by the engine where an imported recording was made.")
