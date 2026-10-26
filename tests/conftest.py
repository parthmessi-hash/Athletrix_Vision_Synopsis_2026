import atexit
import os
import tempfile
from pathlib import Path

# Belt and braces: even if something opened the default database during tests, it would be a
# throwaway file, never the development database under ./data. The folder belongs to this test
# process and is removed when it exits (it is normally empty: every test uses its own tmp_path
# database); nothing else in the temp directory is touched.
_fallback_dir = tempfile.TemporaryDirectory(prefix="athletrix-tests-")
os.environ.setdefault("DATABASE_PATH", str(Path(_fallback_dir.name) / "fallback.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Database, current_database, set_database  # noqa: E402
from app.main import app  # noqa: E402


def close_fallback_database() -> None:
    """Close the process-wide database if it was opened inside this run's fallback folder, so the folder can be removed cleanly.

    A database elsewhere (a test's own tmp_path file, or anything a developer pointed at) is left alone.
    """
    db = current_database()
    if db is not None and not db.closed and db.path != ":memory:" and Path(db.path).resolve().parent == Path(_fallback_dir.name).resolve():
        db.close()


# atexit runs last-registered first: the database is closed, then the folder is removed.
atexit.register(_fallback_dir.cleanup)
atexit.register(close_fallback_database)

TS = "2026-10-15T12:00:00Z"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Every test runs against its own fresh SQLite file; the development database is never touched."""
    db = Database(tmp_path / "test.db")
    set_database(db)
    yield db
    db.close()
    set_database(None)


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
