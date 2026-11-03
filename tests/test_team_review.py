"""The team review: one captured review time and one profile set for every roster athlete, per-athlete failures kept
apart, readings with observation / receipt / age separated. Uses the FICTIONAL demo package."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.api import review as review_api
from app.core.config import Settings
from app.core.recording_package import read_package
from app.store.recording_import import import_package

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("product_demo_team", ROOT / "scripts" / "product_demo.py")
demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
PROFILES = {k: str(v) for k, v in demo.PROFILE_FILES.items()}


@pytest.fixture(scope="module")
def package():
    return read_package(demo.build())


@pytest.fixture
def rid(isolated_db, package, monkeypatch):
    monkeypatch.setattr(review_api, "get_settings", lambda: Settings(physiological_index_profile_path=PROFILES["PHYSIOLOGICAL_INDEX_PROFILE_PATH"],
                                                                     substitution_profile_path=PROFILES["SUBSTITUTION_PROFILE_PATH"],
                                                                     performance_rubric_path=PROFILES["PERFORMANCE_RUBRIC_PATH"]))
    return import_package(isolated_db, package, datetime(2026, 11, 2, tzinfo=UTC)).recording_id


def test_one_request_reviews_the_whole_team_at_one_time(client, rid):
    url = f"/api/v1/recordings/{rid}/team-review"
    assert client.get(url).status_code == 422, "the knowledge mode must be chosen explicitly"
    r = client.get(url, params={"knowledge": "retrospective", "at": demo.z(demo.T_MAIN)}).json()
    assert r["at"] == demo.z(demo.T_MAIN) and r["counts"]["athletes"] == 13 and r["counts"]["reviewed"] == 13 and r["fictional_configuration"] is True
    by = {x["athlete"]["id"]: x for x in r["athletes"]}
    assert [x["athlete"]["id"] for x in r["athletes"]] == [a[0] for a in demo.PLAYERS], "roster order, never ranked"
    single = client.get(f"/api/v1/recordings/{rid}/athletes/p02/substitution-review", params={"at": demo.z(demo.T_MAIN)}).json()
    assert by["p02"]["outcome"] == single["outcome"] == "consider_replacement" and by["p02"]["review"] == single, "the same calculation as the single review"
    assert {x["review"]["profiles"]["substitution"]["sha256"] for x in r["athletes"]} == {r["profiles"]["substitution"]["sha256"]}, "one profile set"
    reading = by["p01"]["latest_reading"]
    assert reading["observed_at"] == demo.z(demo.T_MAIN) and reading["received_at"] is None and reading["availability"].startswith("unknown: no receipt metadata")
    assert by["b02"]["latest_reading"] is None and by["b02"]["roster"]["status"] == "bench" and by["b02"]["physiological_index"]["available"] is False
    assert by["p06"]["latest_reading"]["age_s"] > 1000 and by["p06"]["outcome"] == "insufficient_evidence", "a stale reading keeps its age; it is not shown as current"
    assert by["p02"]["latest_performance_observation"]["entered_at"] > by["p02"]["latest_performance_observation"]["period_end"]


def test_live_mode_uses_only_what_was_entered(client, rid):
    r = client.get(f"/api/v1/recordings/{rid}/team-review", params={"knowledge": "as_entered", "at": demo.z(demo.T_MAIN)}).json()
    p02 = next(x for x in r["athletes"] if x["athlete"]["id"] == "p02")
    assert r["knowledge"] == "as_entered" and p02["review"]["knowledge"]["mode"] == "as_entered"
    assert p02["latest_performance_observation"]["period_end"] < demo.z(demo.T_MAIN), "the annotation of the period ending at 40:00 was entered at 41:00"


def test_a_damaged_record_or_an_exhausted_budget_is_reported_per_athlete(client, rid, isolated_db):
    with isolated_db.transaction() as c:
        c.execute("UPDATE recording_physiological_samples SET payload = '{not json' WHERE recording_id = ? AND athlete_id = 'p04' AND ts_utc = (SELECT MIN(ts_utc) FROM recording_physiological_samples WHERE athlete_id = 'p04')", (rid,))
    r = client.get(f"/api/v1/recordings/{rid}/team-review", params={"knowledge": "retrospective", "at": demo.z(demo.T_MAIN)}).json()
    by = {x["athlete"]["id"]: x for x in r["athletes"]}
    assert by["p04"]["reviewed"] is False and "could not be read" in by["p04"]["error"] and "outcome" not in by["p04"]
    assert by["p03"]["reviewed"] and r["counts"]["not_reviewed"] == 1, "the others are unaffected"
    short = client.get(f"/api/v1/recordings/{rid}/team-review", params={"knowledge": "retrospective", "at": demo.z(demo.T_MAIN), "budget_s": 0.001}).json()
    assert short["counts"]["not_reviewed"] >= 12 and all("time budget" in x["error"] or "could not be read" in x["error"] for x in short["athletes"] if not x["reviewed"])
