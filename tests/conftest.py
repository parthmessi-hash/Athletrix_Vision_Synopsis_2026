import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store.physiological import latest_physiological_store
from app.store.tracking import latest_tracking_store

TS = "2026-10-15T12:00:00Z"


@pytest.fixture(autouse=True)
def clean_stores():
    """Every test starts and ends with empty in-memory stores."""
    latest_physiological_store.clear()
    latest_tracking_store.clear()
    yield
    latest_physiological_store.clear()
    latest_tracking_store.clear()


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def athlete_payload() -> dict:
    return {"id": "ath-1", "name": "Alex Doe", "jersey_number": 10, "sport": "football", "team_id": "team-1"}


@pytest.fixture
def physiological_payload() -> dict:
    return {
        "athlete_id": "ath-1",
        "timestamp": TS,
        "source": "wearable",
        "device_id": "dev-1",
        "heart_rate_bpm": 142,
        "breathing_rate_bpm": 28.5,
        "hrv_rmssd_ms": 35.2,
        "hrv_sdnn_ms": 48.0,
        "activity_level": 0.8,
        "posture": "running",
        "skin_temperature_c": 34.1,
        "spo2_percent": 97,
        "signal_quality": 0.9,
    }


@pytest.fixture
def tracking_payload() -> dict:
    return {
        "athlete_id": "ath-1",
        "timestamp": TS,
        "x_position": 52.3,
        "y_position": 18.7,
        "speed_mps": 6.4,
        "acceleration_mps2": -1.2,
        "tracking_confidence": 0.95,
    }


@pytest.fixture
def device_status_payload() -> dict:
    return {"device_id": "dev-1", "timestamp": TS, "connected": True, "battery_percent": 72, "signal_quality": 0.6}
