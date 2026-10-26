"""Core Athletrix data models.

All models are provider-agnostic: they describe the shape of data the engine
works with, not the format of any particular wearable or camera vendor.
Adapters for specific hardware will map vendor payloads onto these models.
"""

from app.models.athlete import Athlete
from app.models.availability import EffortAvailability, HeartRateAvailability, StreamAvailability, WindowAvailability
from app.models.common import Posture, SampleSource
from app.models.device import DeviceStatus
from app.models.effort import EffortReview, EffortRules, HeartRatePeriod, HeartRatePeriods
from app.models.physiological import PhysiologicalSample
from app.models.recording import (
    ActiveRecording, RecordedAthlete, RecordedData, RecordedStream, Recording, RecordingDetail, RecordingEffortReview, RecordingRef, RecordingStart, RecordingSummary, RecordingWindow,
    SessionEffortAvailability, SessionHeartRateAvailability, SessionStreamAvailability, SessionWindowAvailability,
)
from app.models.retained import RetainedData, StreamRetention
from app.models.sources import SourceBreakdown, WindowSources
from app.models.team import ReportedDevice, TeamAthleteLive, TeamLive
from app.models.live import AthleteLive
from app.models.summary import AthleteSummary, DistanceEstimate, DistanceRules, HighSpeedEstimate, PhysiologicalSummary, TrackingSummary
from app.models.tracking import TrackingSample
from app.models.window import AthleteWindow, TrackingRules

__all__ = [
    "Athlete",
    "AthleteLive",
    "AthleteSummary",
    "AthleteWindow",
    "DistanceEstimate",
    "EffortAvailability",
    "HeartRateAvailability",
    "StreamAvailability",
    "WindowAvailability",
    "DistanceRules",
    "EffortReview",
    "EffortRules",
    "HighSpeedEstimate",
    "HeartRatePeriod",
    "HeartRatePeriods",
    "DeviceStatus",
    "PhysiologicalSample",
    "PhysiologicalSummary",
    "Posture",
    "ActiveRecording",
    "RecordedAthlete",
    "RecordedStream",
    "Recording",
    "RecordingDetail",
    "RecordedData",
    "RecordingEffortReview",
    "RecordingRef",
    "RecordingSummary",
    "RecordingWindow",
    "SessionEffortAvailability",
    "SessionHeartRateAvailability",
    "SessionStreamAvailability",
    "SessionWindowAvailability",
    "RecordingStart",
    "ReportedDevice",
    "RetainedData",
    "SampleSource",
    "SourceBreakdown",
    "StreamRetention",
    "TeamAthleteLive",
    "TeamLive",
    "TrackingRules",
    "TrackingSample",
    "TrackingSummary",
    "WindowSources",
]
