"""Heart-rate CSV converter: explicit optional mappings for the other PhysiologicalSample fields.

A field is written only when its flag is given; blank mapped cells become null; unmapped fields stay absent; values are
taken as written in the model's units and validated with the model. The round trip through the replay proves the fields
reach the history and window endpoints, and that heart-rate summaries still use heart_rate_bpm only. All data is fictional.
"""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "examples" / "fictional_physiology.csv"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = load("convert_heart_rate_csv")
rp = load("replay_samples")

BASE = ["--athlete-id", "athlete-1", "--device-id", "fictional-phys-csv-1", "--source", "simulated"]
ALL_FLAGS = [
    "--breathing-rate-column", "breathing_rate_bpm", "--hrv-rmssd-column", "hrv_rmssd_ms", "--hrv-sdnn-column", "hrv_sdnn_ms",
    "--activity-level-column", "activity_level", "--posture-column", "posture", "--skin-temperature-column", "skin_temperature_c",
    "--spo2-column", "spo2_percent", "--signal-quality-column", "signal_quality",
]
OPTIONAL = ["breathing_rate_bpm", "hrv_rmssd_ms", "hrv_sdnn_ms", "activity_level", "posture", "skin_temperature_c", "spo2_percent", "signal_quality"]
HR_KEYS = {"athlete_id", "timestamp", "source", "device_id", "heart_rate_bpm"}


def run(argv):
    out = []
    code = cv.main(argv, out=out.append)
    return code, "\n".join(out)


def csv_file(tmp_path, text, name="in.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def samples(path):
    return [json.loads(line)["sample"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- fixture with every mapping -------------------------------------------------------------------------------------------

def test_fixture_with_all_mappings_preserves_values_nulls_zeros_and_postures(tmp_path):
    dst = tmp_path / "phys.jsonl"
    code, text = run([str(FIXTURE), str(dst), *BASE, *ALL_FLAGS])
    assert code == 0, text
    rows = samples(dst)
    assert len(rows) == 5 and all(set(r) == HR_KEYS | set(OPTIONAL) for r in rows)
    assert [r["timestamp"] for r in rows] == ["2026-10-20T10:00:00Z", "2026-10-20T12:00:02+02:00", "2026-10-20T10:00:01Z", "2026-10-20T05:00:03-05:00", "2026-10-20T10:00:04Z"]
    assert [r["heart_rate_bpm"] for r in rows] == [112, 115, None, 118, 120.5]
    assert [r["breathing_rate_bpm"] for r in rows] == [18, 19, 0, 20, 21]                 # 0 breaths/min is kept
    assert [r["hrv_rmssd_ms"] for r in rows] == [42.5, None, 40, 38.0, 0]                 # blank -> null, 0 kept
    assert [r["hrv_sdnn_ms"] for r in rows] == [55, None, 52, 50.0, 0]
    assert [r["activity_level"] for r in rows] == [0.0, 0.4, 0.2, 0.6, 0.65]              # 0.0 is a real value
    assert [r["posture"] for r in rows] == ["standing", "walking", None, "running", "unknown"]
    assert [r["skin_temperature_c"] for r in rows] == [33.1, 33.2, 33.1, 33.4, 33.5]
    assert [r["spo2_percent"] for r in rows] == [98, 97.5, None, 97, 96.5]
    assert [r["signal_quality"] for r in rows] == [0.9, 0.85, 1.0, 0.7, 0.6]
    assert all(r["source"] == "simulated" and r["device_id"] == "fictional-phys-csv-1" and r["athlete_id"] == "athlete-1" for r in rows)
    assert "Optional measurements mapped as written, no unit conversion" in text and "hrv_rmssd_ms from column 'hrv_rmssd_ms' (milliseconds (0-1000); 1 null)" in text


def test_heart_rate_only_command_is_unchanged_and_reports_nothing_else(tmp_path):
    dst = tmp_path / "hr.jsonl"
    code, text = run([str(FIXTURE), str(dst), *BASE])
    assert code == 0, text
    rows = samples(dst)
    assert all(set(r) == HR_KEYS for r in rows), "unmapped fields are absent, not null"
    assert [r["heart_rate_bpm"] for r in rows] == [112, 115, None, 118, 120.5]
    assert "No optional measurements mapped: records carry heart rate only" in text


@pytest.mark.parametrize("field,flag,cell,expected", [
    ("breathing_rate_bpm", "--breathing-rate-column", "16", 16),
    ("hrv_rmssd_ms", "--hrv-rmssd-column", "41.25", 41.25),
    ("hrv_sdnn_ms", "--hrv-sdnn-column", "0", 0),
    ("activity_level", "--activity-level-column", "1.0", 1.0),
    ("posture", "--posture-column", "lying", "lying"),
    ("skin_temperature_c", "--skin-temperature-column", "20", 20),
    ("spo2_percent", "--spo2-column", "100", 100),
    ("signal_quality", "--signal-quality-column", "0", 0),
])
def test_each_field_maps_from_a_custom_column_name_and_blank_becomes_null(tmp_path, field, flag, cell, expected):
    src = csv_file(tmp_path, f"t,hr,extra\n2026-10-20T10:00:00Z,100,{cell}\n2026-10-20T10:00:01Z,101,\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([str(src), str(dst), "--timestamp-column", "t", "--heart-rate-column", "hr", *BASE, flag, "extra"])
    assert code == 0, text
    rows = samples(dst)
    assert rows[0][field] == expected and rows[1][field] is None
    assert set(rows[0]) == HR_KEYS | {field}, "only the mapped field is added"


# ---- validation failures ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag,column,cell,needle", [
    ("--spo2-column", "spo2_percent", "101", "row 2, spo2_percent: Input should be less than or equal to 100"),
    ("--activity-level-column", "activity_level", "1.5", "row 2, activity_level: Input should be less than or equal to 1"),
    ("--skin-temperature-column", "skin_temperature_c", "10", "row 2, skin_temperature_c: Input should be greater than or equal to 20"),
    ("--hrv-rmssd-column", "hrv_rmssd_ms", "-1", "row 2, hrv_rmssd_ms: Input should be greater than or equal to 0"),
    ("--breathing-rate-column", "breathing_rate_bpm", "inf", "row 2, breathing_rate_bpm: 'inf' is not a finite number"),
    ("--signal-quality-column", "signal_quality", "nan", "row 2, signal_quality: 'nan' is not a finite number"),
    ("--hrv-sdnn-column", "hrv_sdnn_ms", "fast", "row 2, hrv_sdnn_ms: 'fast' is not a number (hrv_sdnn_ms must already be in milliseconds (0-1000))"),
    ("--posture-column", "posture", "crouching", "row 2, posture: 'crouching' is not an accepted posture (expected one of standing, sitting, lying, walking, running, unknown)"),
    ("--posture-column", "posture", "Standing", "row 2, posture: 'Standing' is not an accepted posture"),
])
def test_invalid_optional_values_are_row_errors_and_nothing_is_written(tmp_path, flag, column, cell, needle):
    src = csv_file(tmp_path, f"timestamp,heart_rate_bpm,{column}\n2026-10-20T10:00:00Z,100,{cell}\n2026-10-20T10:00:01Z,101,\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([str(src), str(dst), *BASE, flag, column])
    assert code == 1 and not dst.exists() and not list(tmp_path.glob(".o.jsonl.*"))
    assert needle in text, text
    assert "1 error(s) on 1 row(s); 1 row(s) were valid." in text


def test_mapped_column_errors_name_the_custom_column(tmp_path):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm,sat\n2026-10-20T10:00:00Z,100,150\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *BASE, "--spo2-column", "sat"])
    assert code == 1 and "row 2, sat: Input should be less than or equal to 100" in text


def test_conflicting_missing_and_duplicated_optional_mappings_are_rejected(tmp_path):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm,rr,rr\n2026-10-20T10:00:00Z,100,40,41\n")
    code, text = run([str(src), str(tmp_path / "a.jsonl"), *BASE, "--hrv-rmssd-column", "heart_rate_bpm"])
    assert code == 1 and "column(s) ['heart_rate_bpm'] are mapped to more than one field (heart_rate_bpm, hrv_rmssd_ms)" in text
    code, text = run([str(src), str(tmp_path / "b.jsonl"), *BASE, "--hrv-rmssd-column", "rmssd", "--hrv-sdnn-column", "rmssd"])
    assert code == 1 and "mapped to more than one field (hrv_rmssd_ms, hrv_sdnn_ms)" in text
    code, text = run([str(src), str(tmp_path / "c.jsonl"), *BASE, "--breathing-rate-column", "breaths"])
    assert code == 1 and "row 1, breaths: column not found in header" in text and "(mapped to breathing_rate_bpm)" in text
    code, text = run([str(src), str(tmp_path / "d.jsonl"), *BASE, "--hrv-rmssd-column", "rr"])
    assert code == 1 and "row 1, rr: column appears 2 times in the header; the mapping is ambiguous" in text
    assert not any(p.suffix == ".jsonl" for p in tmp_path.iterdir())


def test_fields_are_never_guessed_from_matching_headers(tmp_path):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm,spo2_percent,posture\n2026-10-20T10:00:00Z,100,98,standing\n")
    dst = tmp_path / "o.jsonl"
    assert run([str(src), str(dst), *BASE])[0] == 0
    assert set(samples(dst)[0]) == HR_KEYS, "columns named like model fields are ignored without their flag"


def test_dry_run_with_mappings_validates_and_writes_nothing(tmp_path):
    dst = tmp_path / "o.jsonl"
    code, text = run([str(FIXTURE), str(dst), *BASE, *ALL_FLAGS, "--dry-run"])
    assert code == 0 and not dst.exists() and "Dry run: the CSV is valid; no file was written." in text
    bad = csv_file(tmp_path, "timestamp,heart_rate_bpm,posture\n2026-10-20T10:00:00Z,100,flying\n")
    code, text = run([str(bad), str(tmp_path / "p.jsonl"), *BASE, "--posture-column", "posture", "--dry-run"])
    assert code == 1 and "row 2, posture" in text and not (tmp_path / "p.jsonl").exists()


def test_existing_output_is_still_never_overwritten(tmp_path):
    dst = tmp_path / "o.jsonl"
    dst.write_text("keep me\n", encoding="utf-8")
    code, text = run([str(FIXTURE), str(dst), *BASE, *ALL_FLAGS])
    assert code == 1 and "already exists" in text and dst.read_text(encoding="utf-8") == "keep me\n"


def test_convert_rows_rejects_unknown_optional_field_names():
    converted, errors = cv.convert_rows([["timestamp", "heart_rate_bpm"], ["2026-10-20T10:00:00Z", "100"]], timestamp_column="timestamp", heart_rate_column="heart_rate_bpm",
                                        athlete_id="a", device_id="d", source="simulated", optional_columns={"fatigue_score": "x"})
    assert converted == [] and "unknown optional field(s) ['fatigue_score']" in str(errors[0])


# ---- round trip --------------------------------------------------------------------------------------------------------

class Borrowed:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def test_round_trip_keeps_optional_fields_and_heart_rate_summary_uses_heart_rate_only(client, tmp_path):
    dst = tmp_path / "phys.jsonl"
    assert run([str(FIXTURE), str(dst), *BASE, *ALL_FLAGS])[0] == 0
    recs, errors = rp.load_file(dst)
    assert errors == [] and len(recs) == 5
    assert rp.main([str(dst)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=lambda s: None) == 0
    hist = client.get("/api/v1/athletes/athlete-1/physiological/history?limit=10").json()
    assert [s["breathing_rate_bpm"] for s in hist] == [18, 0, 19, 20, 21]          # chronological; the 0 survives
    assert [s["posture"] for s in hist] == ["standing", None, "walking", "running", "unknown"]
    assert [s["hrv_rmssd_ms"] for s in hist] == [42.5, 40, None, 38.0, 0]
    assert [s["activity_level"] for s in hist] == [0.0, 0.2, 0.4, 0.6, 0.65]
    assert [s["spo2_percent"] for s in hist] == [98, None, 97.5, 97, 96.5]
    assert [s["signal_quality"] for s in hist] == [0.9, 1.0, 0.85, 0.7, 0.6]
    assert [s["skin_temperature_c"] for s in hist] == [33.1, 33.1, 33.2, 33.4, 33.5]
    assert [s["timestamp"] for s in hist] == ["2026-10-20T10:00:00Z", "2026-10-20T10:00:01Z", "2026-10-20T12:00:02+02:00", "2026-10-20T05:00:03-05:00", "2026-10-20T10:00:04Z"]
    win = client.get("/api/v1/athletes/athlete-1/window?seconds=60&end=2026-10-20T10:00:04Z").json()
    assert [s["posture"] for s in win["physiological_samples"]] == ["standing", None, "walking", "running", "unknown"]
    assert win["sources"]["physiological"] == {"reported": {"simulated": 5}, "not_reported": 0}
    s = client.get("/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:04Z").json()["physiological"]
    assert s["sample_count"] == 5 and s["heart_rate_count"] == 4
    assert s["heart_rate_mean_bpm"] == pytest.approx((112 + 115 + 118 + 120.5) / 4) and s["heart_rate_peak_bpm"] == 120.5, "other measurements do not enter the heart-rate figures"
    assert set(s) == {"sample_count", "heart_rate_count", "heart_rate_mean_bpm", "heart_rate_peak_bpm", "first_sample_at", "last_sample_at", "latest_sample_age_seconds", "max_inter_sample_gap_seconds", "sources"}, "no new summary metric was added"
