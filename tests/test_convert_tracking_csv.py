"""Tracking CSV converter: valid conversion, column mapping, errors with row numbers, zeros, timezones, dry run, output protection.

Also proves the converter's output is valid input for the replay script and, together with the heart-rate converter,
survives the round trip into the app (isolated temporary database) and back out through a recorded window and summary,
with the distance and high-speed figures checked against values calculated by hand.
"""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSV_FIXTURE = ROOT / "scripts" / "examples" / "fictional_tracking.csv"
HR_FIXTURE = ROOT / "scripts" / "examples" / "fictional_heart_rate.csv"
HEADER = "timestamp,x_position,y_position,speed_mps,acceleration_mps2,tracking_confidence"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ct = load("convert_tracking_csv")
hr = load("convert_heart_rate_csv")
rp = load("replay_samples")

ARGS = ["--athlete-id", "athlete-1"]


def run(argv):
    out = []
    code = ct.main(argv, out=out.append)
    return code, "\n".join(out)


def csv_file(tmp_path, text, name="in.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- valid conversion -----------------------------------------------------------------------------------------

def test_fixture_converts_preserving_order_text_zeros_and_negative_acceleration(tmp_path):
    dst = tmp_path / "out.jsonl"
    code, text = run([str(CSV_FIXTURE), str(dst), *ARGS])
    assert code == 0, text
    recs = records(dst)
    assert [r["type"] for r in recs] == ["tracking"] * 5
    samples = [r["sample"] for r in recs]
    assert [s["timestamp"] for s in samples] == ["2026-10-20T10:00:00Z", "2026-10-20T12:00:02+02:00", "2026-10-20T10:00:01Z", "2026-10-20T05:00:03-05:00", "2026-10-20T10:00:04Z"]  # CSV order, text as written
    assert [s["x_position"] for s in samples] == [10.0, 15.0, 11.5, 20.0, 25.5]
    assert [s["y_position"] for s in samples] == [5.0, 4.5, 5.0, 4.0, 4.0]
    assert [s["speed_mps"] for s in samples] == [0.0, 4.0, 3.0, 6.0, 5.0]                  # the stationary zero is a real value
    assert [s["acceleration_mps2"] for s in samples] == [0.0, 1.0, 3.0, 2.0, -1.0]         # negative acceleration preserved
    assert [s["tracking_confidence"] for s in samples] == [0.9, 0.95, 0.3, 0.9, 0.85]      # low confidence written as recorded, not altered
    assert all(s["athlete_id"] == "athlete-1" for s in samples)
    assert all(set(s) == {"athlete_id", "timestamp", "x_position", "y_position", "speed_mps", "acceleration_mps2", "tracking_confidence"} for s in samples)  # no source/device, nothing invented
    assert "TRACKING CSV CONVERSION" in text and "5 tracking records" in text
    assert "1 row(s) with speed 0; 1 row(s) with negative acceleration" in text
    assert "tracking_confidence as written: 0.3 to 0.95" in text
    assert "1 row(s) are earlier than the row before them" in text
    assert "2026-10-20T10:00:00+00:00 to 2026-10-20T10:00:04+00:00 (span 4.000 s)" in text


def test_output_is_valid_replay_input(tmp_path):
    dst = tmp_path / "out.jsonl"
    assert run([str(CSV_FIXTURE), str(dst), *ARGS])[0] == 0
    recs, errors = rp.load_file(dst)
    assert errors == [] and len(recs) == 5 and all(r.type == "tracking" for r in recs)
    assert [r.line for r in rp.order_records(recs)] == [1, 3, 2, 4, 5]  # the replay puts the 10:00:01Z row before the +02:00 (10:00:02Z) row


def test_custom_column_mapping_and_extra_columns_ignored(tmp_path):
    src = csv_file(tmp_path, "id,conf,accel,speed,y,x,time,note\n7,0.8,-0.25,1.5,2,-3.5,2026-10-20T10:00:00Z,x\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([
        str(src), str(dst), "--athlete-id", "a", "--timestamp-column", "time", "--x-position-column", "x", "--y-position-column", "y",
        "--speed-mps-column", "speed", "--acceleration-mps2-column", "accel", "--tracking-confidence-column", "conf",
    ])
    assert code == 0, text
    assert records(dst)[0]["sample"] == {"athlete_id": "a", "timestamp": "2026-10-20T10:00:00Z", "x_position": -3.5, "y_position": 2, "speed_mps": 1.5, "acceleration_mps2": -0.25, "tracking_confidence": 0.8}


def test_bom_blank_lines_and_crlf_are_handled(tmp_path):
    src = tmp_path / "bom.csv"
    src.write_bytes(b"\xef\xbb\xbf" + HEADER.encode() + b"\r\n2026-10-20T10:00:00Z,1,2,3,4,0.5\r\n\r\n2026-10-20T10:00:01Z,1,2,3,4,1\r\n")
    dst = tmp_path / "o.jsonl"
    assert run([str(src), str(dst), *ARGS])[0] == 0
    assert [s["sample"]["tracking_confidence"] for s in records(dst)] == [0.5, 1]


# ---- zero handling ---------------------------------------------------------------------------------------------------

def test_all_zero_row_and_zero_confidence_are_real_values(tmp_path):
    src = csv_file(tmp_path, HEADER + "\n2026-10-20T10:00:00Z,0,0,0,0,0\n2026-10-20T10:00:01Z,0.0,-0.0,0.0,-0.0,1.0\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([str(src), str(dst), *ARGS])
    assert code == 0, text
    a, b = [r["sample"] for r in records(dst)]
    assert (a["x_position"], a["y_position"], a["speed_mps"], a["acceleration_mps2"], a["tracking_confidence"]) == (0, 0, 0, 0, 0)
    assert b["speed_mps"] == 0.0 and b["acceleration_mps2"] == 0.0 and b["tracking_confidence"] == 1.0
    assert "2 row(s) with speed 0" in text


# ---- errors: whole file validated, nothing written ---------------------------------------------------------------

def test_errors_reported_with_row_numbers_and_columns_and_no_output(tmp_path):
    src = csv_file(tmp_path, "\n".join([
        HEADER,
        "2026-10-20T10:00:00Z,1,2,3,4,0.5",          # row 2 valid
        "2026-10-20T10:00:01,1,2,3,4,0.5",           # row 3 naive
        "1760954401,1,2,3,4,0.5",                    # row 4 epoch
        "2026-13-01T10:00:00Z,1,2,3,4,0.5",          # row 5 invalid date
        "2026-10-20T10:00:05Z,abc,2,3,4,0.5",        # row 6 not a number
        "2026-10-20T10:00:06Z,1,2,-3,4,0.5",         # row 7 negative speed (model)
        "2026-10-20T10:00:07Z,1,2,3,4,1.5",          # row 8 confidence above 1 (model)
        "2026-10-20T10:00:08Z,1,2,inf,4,0.5",        # row 9 not finite
        "2026-10-20T10:00:09Z,1,2,3,nan,0.5",        # row 10 not finite
        "2026-10-20T10:00:10Z,1,2,3,4,0.5,extra",    # row 11 wrong field count
        "2026-10-20T10:00:11Z,1,2,3,4,",             # row 12 missing confidence: never filled with 1
        "2026-10-20T10:00:12Z,1,2,,4,0.5",           # row 13 missing speed: never calculated
        "2026-10-20 10:00:13Z,1,2,3,4,0.5",          # row 14 space separator: not accepted
    ]) + "\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([str(src), str(dst), *ARGS])
    assert code == 1 and not dst.exists()
    assert not list(tmp_path.glob(".o.jsonl.*")), "no temporary output left behind"
    assert "nothing was written" in text
    for needle in [
        "row 3, timestamp: must be ISO 8601 with an explicit timezone",
        "row 4, timestamp: numeric epoch values are not accepted",
        "row 5, timestamp: not a valid date-time",
        "row 6, x_position: 'abc' is not a number (x_position must already be in metres)",
        "row 7, speed_mps: Input should be greater than or equal to 0",
        "row 8, tracking_confidence: Input should be less than or equal to 1",
        "row 9, speed_mps: 'inf' is not a finite number",
        "row 10, acceleration_mps2: 'nan' is not a finite number",
        "row 11, row: has 7 fields, header has 6",
        "row 12, tracking_confidence: value is missing; tracking_confidence is required and is never filled in or calculated",
        "row 13, speed_mps: value is missing; speed_mps is required and is never filled in or calculated",
        "row 14, timestamp: must be ISO 8601",
    ]:
        assert needle in text, needle
    assert "12 error(s) on 12 row(s); 1 row(s) were valid." in text


def test_several_errors_on_one_row_are_all_reported(tmp_path):
    src = csv_file(tmp_path, HEADER + "\n2026-10-20T10:00:00,x,,3,4,2\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *ARGS])
    assert code == 1
    assert "row 2, timestamp: must be ISO 8601" in text and "row 2, x_position: 'x' is not a number" in text
    assert "row 2, y_position: value is missing" in text
    assert "3 error(s) on 1 row(s)" in text  # the model is not consulted until the cells parse, so the confidence range error waits


def test_model_errors_name_the_mapped_csv_column(tmp_path):
    src = csv_file(tmp_path, "t,x,y,v,a,c\n2026-10-20T10:00:00Z,1,2,-1,0,0.5\n")
    code, text = run([
        str(src), str(tmp_path / "o.jsonl"), *ARGS, "--timestamp-column", "t", "--x-position-column", "x", "--y-position-column", "y",
        "--speed-mps-column", "v", "--acceleration-mps2-column", "a", "--tracking-confidence-column", "c",
    ])
    assert code == 1 and "row 2, v: Input should be greater than or equal to 0" in text


@pytest.mark.parametrize("header,expect", [
    ("time,x_position,y_position,speed_mps,acceleration_mps2,tracking_confidence", "row 1, timestamp: column not found in header"),
    ("timestamp,x_position,y_position,speed_mps,acceleration_mps2", "row 1, tracking_confidence: column not found in header"),
    ("timestamp,x_position,y_position,speed_mps,acceleration_mps2,tracking_confidence,speed_mps", "row 1, speed_mps: column appears 2 times in the header; the mapping is ambiguous"),
    ("timestamp,timestamp,y_position,speed_mps,acceleration_mps2,tracking_confidence", "row 1, timestamp: column appears 2 times"),
    (",,,,,", "row 1, row: header row is empty"),
])
def test_missing_or_ambiguous_headers(tmp_path, header, expect):
    src = csv_file(tmp_path, header + "\n2026-10-20T10:00:00Z,1,2,3,4,0.5,6\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *ARGS])
    assert code == 1 and expect in text and not (tmp_path / "o.jsonl").exists()


def test_mapped_column_missing_names_the_field(tmp_path):
    src = csv_file(tmp_path, HEADER + "\n2026-10-20T10:00:00Z,1,2,3,4,0.5\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *ARGS, "--speed-mps-column", "velocity"])
    assert code == 1 and "row 1, velocity: column not found in header" in text and "(needed for speed_mps)" in text


def test_same_column_for_two_fields_is_rejected(tmp_path):
    src = csv_file(tmp_path, HEADER + "\n2026-10-20T10:00:00Z,1,2,3,4,0.5\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *ARGS, "--x-position-column", "speed_mps"])
    assert code == 1 and "named for more than one field (x_position, speed_mps)" in text


def test_empty_file_header_only_and_missing_file(tmp_path):
    assert "file is empty" in run([str(csv_file(tmp_path, "")), str(tmp_path / "a.jsonl"), *ARGS])[1]
    assert "header but no data rows" in run([str(csv_file(tmp_path, HEADER + "\n")), str(tmp_path / "b.jsonl"), *ARGS])[1]
    assert "Cannot read" in run([str(tmp_path / "missing.csv"), str(tmp_path / "c.jsonl"), *ARGS])[1]


def test_athlete_id_is_required_and_no_source_or_device_options_exist(tmp_path):
    src = csv_file(tmp_path, HEADER + "\n2026-10-20T10:00:00Z,1,2,3,4,0.5\n")
    with pytest.raises(SystemExit) as exc:
        ct.main([str(src), str(tmp_path / "o.jsonl")])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        ct.main([str(src), str(tmp_path / "o.jsonl"), *ARGS, "--source", "camera"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        ct.main([str(src), str(tmp_path / "o.jsonl"), *ARGS, "--device-id", "cam-1"])
    assert exc.value.code == 2


# ---- timezone handling -----------------------------------------------------------------------------------------------

def test_offsets_are_preserved_textually_and_compared_as_instants(tmp_path):
    src = csv_file(tmp_path, HEADER + "\n2026-10-20T12:00:00+02:00,1,2,3,4,0.5\n2026-10-20T10:00:00Z,1,2,3,4,0.5\n2026-10-20T05:00:00.250-05:00,1,2,3,4,0.5\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([str(src), str(dst), *ARGS])
    assert code == 0
    assert [s["sample"]["timestamp"] for s in records(dst)] == ["2026-10-20T12:00:00+02:00", "2026-10-20T10:00:00Z", "2026-10-20T05:00:00.250-05:00"]
    assert "span 0.250 s" in text  # the first two are the same instant


# ---- dry run and output protection ---------------------------------------------------------------------------------

def test_dry_run_validates_and_writes_nothing(tmp_path):
    dst = tmp_path / "o.jsonl"
    code, text = run([str(CSV_FIXTURE), str(dst), *ARGS, "--dry-run"])
    assert code == 0 and not dst.exists() and not [p for p in tmp_path.iterdir() if p.suffix in (".jsonl", ".part")]
    assert "Dry run: the CSV is valid; no file was written." in text and "5 tracking records" in text
    bad = csv_file(tmp_path, HEADER + "\n2026-10-20T10:00:00Z,1,2,3,4,\n")
    code, text = run([str(bad), str(tmp_path / "p.jsonl"), *ARGS, "--dry-run"])
    assert code == 1 and "row 2, tracking_confidence" in text and not (tmp_path / "p.jsonl").exists()


def test_existing_output_is_never_overwritten(tmp_path):
    dst = tmp_path / "o.jsonl"
    dst.write_text("keep me\n", encoding="utf-8")
    code, text = run([str(CSV_FIXTURE), str(dst), *ARGS])
    assert code == 1 and "already exists" in text and dst.read_text(encoding="utf-8") == "keep me\n"
    code, _ = run([str(CSV_FIXTURE), str(dst), *ARGS, "--dry-run"])
    assert code == 1 and dst.read_text(encoding="utf-8") == "keep me\n"


def test_output_written_atomically_into_a_new_folder(tmp_path):
    dst = tmp_path / "nested" / "out.jsonl"
    assert run([str(CSV_FIXTURE), str(dst), *ARGS])[0] == 0
    assert dst.exists() and sorted(p.name for p in dst.parent.iterdir()) == ["out.jsonl"]  # no .part file left


def test_converter_makes_no_network_calls():
    import inspect
    for mod in (ct, hr):  # the tracking converter reuses the heart-rate converter's helpers
        source = inspect.getsource(mod)
        assert "httpx" not in source and "urllib" not in source and "socket" not in source


# ---- round trip: both converters -> replay -> app -> recorded window and summary -----------------------------------------

class Borrowed:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def test_round_trip_of_both_fixtures_through_replay_window_and_summary(client, tmp_path):
    trk = tmp_path / "trk.jsonl"
    hrj = tmp_path / "hr.jsonl"
    assert run([str(CSV_FIXTURE), str(trk), *ARGS])[0] == 0
    assert hr.main([str(HR_FIXTURE), str(hrj), "--athlete-id", "athlete-1", "--device-id", "fictional-hr-csv-1", "--source", "simulated"], out=lambda s: None) == 0
    for path in (trk, hrj):
        assert rp.main([str(path)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=lambda s: None) == 0

    win = client.get("/api/v1/athletes/athlete-1/window?seconds=60&end=2026-10-20T10:00:04Z").json()
    assert datetime.fromisoformat(win["window_start"]) == datetime(2026, 10, 20, 9, 59, 4, tzinfo=UTC)
    assert datetime.fromisoformat(win["window_end"]) == datetime(2026, 10, 20, 10, 0, 4, tzinfo=UTC)
    assert len(win["physiological_samples"]) == 5 and len(win["tracking_samples"]) == 5
    assert [s["heart_rate_bpm"] for s in win["physiological_samples"]] == [112, None, 115, 118, 120.5]  # chronological; the null survives
    track = win["tracking_samples"]
    assert [s["timestamp"] for s in track] == ["2026-10-20T10:00:00Z", "2026-10-20T10:00:01Z", "2026-10-20T12:00:02+02:00", "2026-10-20T05:00:03-05:00", "2026-10-20T10:00:04Z"]
    assert [s["speed_mps"] for s in track] == [0.0, 3.0, 4.0, 6.0, 5.0]
    assert [s["acceleration_mps2"] for s in track] == [0.0, 3.0, 1.0, 2.0, -1.0]
    assert [s["tracking_confidence"] for s in track] == [0.9, 0.3, 0.95, 0.9, 0.85]
    assert [(s["x_position"], s["y_position"]) for s in track] == [(10.0, 5.0), (11.5, 5.0), (15.0, 4.5), (20.0, 4.0), (25.5, 4.0)]
    assert win["tracking_rules"]["min_confidence"] == 0.5
    # Both streams share the window's bounds: every sample lies within [window_start, window_end].
    start, end = datetime.fromisoformat(win["window_start"]), datetime.fromisoformat(win["window_end"])
    for s in win["physiological_samples"] + track:
        assert start <= datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00")) <= end

    summary = client.get("/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:04Z").json()
    assert summary["window_start"] == win["window_start"] and summary["window_end"] == win["window_end"]
    p, t = summary["physiological"], summary["tracking"]
    assert p["sample_count"] == 5 and p["heart_rate_count"] == 4 and p["heart_rate_mean_bpm"] == pytest.approx(116.375) and p["heart_rate_peak_bpm"] == 120.5
    assert t["sample_count"] == 5 and t["speed_peak_mps"] == 6.0 and t["latest_sample_age_seconds"] == 0.0 and t["max_inter_sample_gap_seconds"] == 1.0
    assert t["distance_rules"] == {"max_gap_seconds": 3.0, "min_confidence": 0.5, "high_speed_threshold_mps": 5.0}
    # Hand calculation with the default rules. Intervals: 0->1 s and 1->2 s are excluded (the 10:00:01Z sample has confidence 0.3);
    # 2->3 s: (4 + 6) / 2 * 1 = 5.0 m; 3->4 s: (6 + 5) / 2 * 1 = 5.5 m. Total 10.5 m over 2.0 s of a 60 s window.
    assert t["estimated_distance_m"] == pytest.approx(10.5)
    assert t["included_duration_seconds"] == pytest.approx(2.0) and t["included_window_fraction"] == pytest.approx(2.0 / 60)
    assert t["excluded_interval_count"] == 2
    # High speed (>= 5 m/s): 2->3 s rises 4 -> 6, crossing at 0.5 s, so 0.5 s and (5 + 6) / 2 * 0.5 = 2.75 m count;
    # 3->4 s stays at or above 5 for the full 1.0 s and 5.5 m. Total 1.5 s and 8.25 m.
    assert t["high_speed_duration_seconds"] == pytest.approx(1.5) and t["high_speed_distance_m"] == pytest.approx(8.25)

    live = client.get("/api/v1/athletes/athlete-1/summary?seconds=60").json()
    assert live["physiological"]["sample_count"] == 0 and live["tracking"]["sample_count"] == 0  # historical, not live
    live_win = client.get("/api/v1/athletes/athlete-1/window?seconds=60").json()
    assert live_win["physiological_samples"] == [] and live_win["tracking_samples"] == []
