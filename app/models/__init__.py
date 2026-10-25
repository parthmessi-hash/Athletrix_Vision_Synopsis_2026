"""Core Athletrix data models.

All models are provider-agnostic: they describe the shape of data the engine
works with, not the format of any particular wearable or camera vendor.
Adapters for specific hardware will map vendor payloads onto these models.
"""

from app.models.athlete import Athlete
from app.models.common import Posture, SampleSource
from app.models.device import DeviceStatus
from app.models.physiological import PhysiologicalSample
from app.models.summary import AthleteSummary, DistanceEstimate, DistanceRules, HighSpeedEstimate, PhysiologicalSummary, TrackingSummary
from app.models.tracking import TrackingSample
from app.models.window import AthleteWindow, TrackingRules

__all__ = [
    "Athlete",
    "AthleteSummary",
    "AthleteWindow",
    "DistanceEstimate",
    "DistanceRules",
    "HighSpeedEstimate",
    "DeviceStatus",
    "PhysiologicalSample",
    "PhysiologicalSummary",
    "Posture",
    "SampleSource",
    "TrackingRules",
    "TrackingSample",
    "TrackingSummary",
]
