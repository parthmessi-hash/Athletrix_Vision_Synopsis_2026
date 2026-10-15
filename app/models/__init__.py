"""Core Athletrix data models.

All models are provider-agnostic: they describe the shape of data the engine
works with, not the format of any particular wearable or camera vendor.
Adapters for specific hardware will map vendor payloads onto these models.
"""

from app.models.athlete import Athlete
from app.models.common import Posture, SampleSource
from app.models.device import DeviceStatus
from app.models.physiological import PhysiologicalSample
from app.models.tracking import TrackingSample

__all__ = [
    "Athlete",
    "DeviceStatus",
    "PhysiologicalSample",
    "Posture",
    "SampleSource",
    "TrackingSample",
]
