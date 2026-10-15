"""Direct model validation tests (no HTTP layer)."""

import pytest
from pydantic import ValidationError

from app.models import Athlete, DeviceStatus, PhysiologicalSample, Posture, SampleSource, TrackingSample


# ---- Athlete -------------------------------------------------------------

def test_athlete_valid(athlete_payload):
    athlete = Athlete(**athlete_payload)
    assert athlete.jersey_number == 10
    assert athlete.sport == "football"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"name": ""},
        {"jersey_number": -1},
        {"jersey_number": "ten"},
        {"sport": ""},
        {"team_id": ""},
        {"unknown_field": "x"},
    ],
)
def test_athlete_invalid(athlete_payload, overrides):
    with pytest.raises(ValidationError):
        Athlete(**{**athlete_payload, **overrides})


def test_athlete_missing_required(athlete_payload):
    del athlete_payload["team_id"]
    with pytest.raises(ValidationError):
        Athlete(**athlete_payload)


# ---- PhysiologicalSample -------------------------------------------------

def test_physiological_valid_full(physiological_payload):
    sample = PhysiologicalSample(**physiological_payload)
    assert sample.source is SampleSource.WEARABLE
    assert sample.posture is Posture.RUNNING
    assert sample.timestamp.tzinfo is not None


def test_physiological_valid_minimal():
    sample = PhysiologicalSample(
        athlete_id="ath-1", timestamp="2026-10-15T12:00:00+00:00", source="camera", device_id="cam-1"
    )
    assert sample.heart_rate_bpm is None
    assert sample.posture is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"timestamp": "2026-10-15T12:00:00"},  # naive datetime, no timezone
        {"timestamp": "not-a-date"},
        {"source": "acme-band-3000"},  # vendor name, not a generic source
        {"device_id": ""},
        {"heart_rate_bpm": 0},
        {"heart_rate_bpm": 400},
        {"breathing_rate_bpm": -1},
        {"hrv_rmssd_ms": -5},
        {"activity_level": 1.5},
        {"posture": "crouching"},
        {"skin_temperature_c": 60},
        {"spo2_percent": 101},
        {"signal_quality": -0.1},
    ],
)
def test_physiological_invalid(physiological_payload, overrides):
    with pytest.raises(ValidationError):
        PhysiologicalSample(**{**physiological_payload, **overrides})


# ---- TrackingSample ------------------------------------------------------

def test_tracking_valid(tracking_payload):
    sample = TrackingSample(**tracking_payload)
    assert sample.acceleration_mps2 == -1.2  # negative acceleration is allowed


def test_tracking_negative_positions_allowed(tracking_payload):
    sample = TrackingSample(**{**tracking_payload, "x_position": -3.0, "y_position": -0.5})
    assert sample.x_position == -3.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"speed_mps": -0.1},
        {"tracking_confidence": 1.01},
        {"tracking_confidence": -0.01},
        {"x_position": "far"},
        {"timestamp": "2026-10-15T12:00:00"},
    ],
)
def test_tracking_invalid(tracking_payload, overrides):
    with pytest.raises(ValidationError):
        TrackingSample(**{**tracking_payload, **overrides})


def test_tracking_missing_required(tracking_payload):
    del tracking_payload["speed_mps"]
    with pytest.raises(ValidationError):
        TrackingSample(**tracking_payload)


# ---- DeviceStatus --------------------------------------------------------

def test_device_status_valid(device_status_payload):
    status = DeviceStatus(**device_status_payload)
    assert status.connected is True


def test_device_status_minimal():
    status = DeviceStatus(device_id="dev-1", timestamp="2026-10-15T12:00:00Z", connected=False)
    assert status.battery_percent is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"device_id": ""},
        {"connected": "maybe"},
        {"battery_percent": 101},
        {"battery_percent": -1},
        {"signal_quality": 2},
    ],
)
def test_device_status_invalid(device_status_payload, overrides):
    with pytest.raises(ValidationError):
        DeviceStatus(**{**device_status_payload, **overrides})
