"""The window response carries the applied tracking-confidence rule, read from the distance settings.

The frontend timeline classifies tracking samples from the window response alone, so the rule must be
in that response, must be the configured DISTANCE_MIN_CONFIDENCE (not a second default), and must match
what the summary endpoint applies to the same athlete.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.api import athletes as athletes_api
from app.core import clock
from app.core.config import Settings, get_settings
from app.models import AthleteWindow, TrackingRules

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
TRACK = "/api/v1/samples/tracking"
WINDOW = "/api/v1/athletes/ath-1/window?seconds=60"
SUMMARY = "/api/v1/athletes/ath-1/summary?seconds=60"


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: NOW)


@pytest.fixture
def settings_override(monkeypatch):
    """Replace the settings the athlete API reads with a given configuration for one test (no .env, no cache leak)."""
    def apply(**overrides):
        monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
        custom = Settings(**overrides)
        monkeypatch.setattr(athletes_api, "get_settings", lambda: custom)
        return custom
    return apply


def post_track(client, base, seconds, conf):
    r = client.post(TRACK, json={**base, "timestamp": at(seconds).isoformat(), "tracking_confidence": conf})
    assert r.status_code == 200, r.text


# ---- model ------------------------------------------------------------------

def test_tracking_rules_model_bounds_and_strictness():
    assert TrackingRules(min_confidence=0).min_confidence == 0
    assert TrackingRules(min_confidence=1).min_confidence == 1
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            TrackingRules(min_confidence=bad)
    with pytest.raises(ValidationError):
        TrackingRules(min_confidence=0.5, max_gap_seconds=3)  # unknown fields rejected: the block stays small
    with pytest.raises(ValidationError):
        TrackingRules()  # the rule is required, never silently defaulted


def test_window_model_requires_tracking_rules():
    with pytest.raises(ValidationError):
        AthleteWindow(athlete_id="a", window_start=at(-60), window_end=at(0), physiological_samples=[], tracking_samples=[])


# ---- endpoint: default configuration ------------------------------------------

def test_window_carries_default_rule_and_matches_summary(client, tracking_payload):
    post_track(client, tracking_payload, -1, 0.9)
    body = client.get(WINDOW).json()
    assert body["tracking_rules"] == {"min_confidence": get_settings().distance_min_confidence}
    assert body["tracking_rules"]["min_confidence"] == 0.5  # the documented DISTANCE_MIN_CONFIDENCE default
    summary = client.get(SUMMARY).json()
    assert summary["tracking"]["distance_rules"]["min_confidence"] == body["tracking_rules"]["min_confidence"]


def test_empty_window_still_carries_rule(client):
    body = client.get("/api/v1/athletes/nobody/window?seconds=10").json()
    assert body["tracking_samples"] == [] and body["physiological_samples"] == []
    assert body["tracking_rules"] == {"min_confidence": 0.5}


def test_window_response_is_self_contained(client, tracking_payload):
    post_track(client, tracking_payload, -2, 0.3)   # below the rule: still returned, the consumer classifies it
    post_track(client, tracking_payload, -1, 0.5)   # exactly at the rule
    body = client.get(WINDOW).json()
    assert set(body) == {"athlete_id", "window_start", "window_end", "physiological_samples", "tracking_samples", "tracking_rules", "sources"}
    assert [s["tracking_confidence"] for s in body["tracking_samples"]] == [0.3, 0.5]
    assert datetime.fromisoformat(body["window_end"]) == NOW  # one captured backend window end, unchanged


# ---- endpoint: custom configuration -------------------------------------------

@pytest.mark.parametrize("value", [0.0, 0.25, 0.8, 1.0])
def test_window_reports_the_configured_rule(client, settings_override, value):
    settings_override(distance_min_confidence=value)
    body = client.get(WINDOW).json()
    assert body["tracking_rules"] == {"min_confidence": value}


def test_window_and_summary_apply_the_same_custom_rule(client, tracking_payload, settings_override):
    settings_override(distance_min_confidence=0.8)
    post_track(client, tracking_payload, -2, 0.7)   # usable under the default 0.5, not under 0.8
    post_track(client, tracking_payload, -1, 0.9)
    win = client.get(WINDOW).json()
    summ = client.get(SUMMARY).json()["tracking"]
    assert win["tracking_rules"]["min_confidence"] == 0.8
    assert summ["distance_rules"]["min_confidence"] == 0.8
    assert summ["estimated_distance_m"] is None and summ["excluded_interval_count"] == 1  # the 0.7 endpoint fails the same rule


def test_rule_comes_from_environment_setting(client, monkeypatch):
    """The real settings path: DISTANCE_MIN_CONFIDENCE in the environment reaches the window response."""
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    monkeypatch.setenv("DISTANCE_MIN_CONFIDENCE", "0.65")
    get_settings.cache_clear()
    try:
        assert client.get(WINDOW).json()["tracking_rules"] == {"min_confidence": 0.65}
    finally:
        get_settings.cache_clear()  # never leave the overridden settings cached for later tests
