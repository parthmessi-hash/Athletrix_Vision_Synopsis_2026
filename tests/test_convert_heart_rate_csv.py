"""Heart-rate CSV converter: valid conversion, errors with row numbers, headers, timezones, nulls, dry run, output protection.

Also proves the converter's output is valid input for the replay script and survives the round trip into the
app (isolated temporary database) and back out through a recorded-review summary.
"""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSV_FIXTURE = ROOT / "scripts" / "examples" / "fictional_heart_rate.csv"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = load("convert_heart_rate_csv")
rp = load("replay_samples")

ARGS = ["--athlete-id", "athlete-1", "--device-id", "fictional-hr-csv-1", "--source", "simulated"]


def run(argv):
    out = []
    code = cv.main(argv, out=out.append)
    return code, "\n".join(out)


def csv_file(tmp_path, text, name="in.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- valid conversion -----------------------------------------------------------------------------------------

def test_fixture_converts_preserving_timestamps_offsets_values_and_nulls(tmp_path):
    dst = tmp_path / "out.jsonl"
    code, text = run([str(CSV_FIXTURE), str(dst), *ARGS])
    assert code == 0, text
    recs = records(dst)
    assert [r["type"] for r in recs] == ["physiological"] * 5
    samples = [r["sample"] for r in recs]
    assert [s["timestamp"] for s in samples] == ["2026-10-20T10:00:00Z", "2026-10-20T12:00:02+02:00", "2026-10-20T10:00:01Z", "2026-10-20T05:00:03-05:00", "2026-10-20T10:00:04Z"]  # CSV order, text as written
    assert [s["heart_rate_bpm"] for s in samples] == [112, 115, None, 118, 120.5]       # blank -> null, never 0; integers stay integers
    assert all(s["athlete_id"] == "athlete-1" and s["device_id"] == "fictional-hr-csv-1" and s["source"] == "simulated" for s in samples)
    assert all(set(s) == {"athlete_id", "timestamp", "source", "device_id", "heart_rate_bpm"} for s in samples)  # nothing invented
    assert "HEART-RATE CSV CONVERSION" in text and "5 physiological records (1 with heart rate null)" in text
    assert "1 row(s) are earlier than the row before them" in text
    assert "2026-10-20T10:00:00+00:00 to 2026-10-20T10:00:04+00:00 (span 4.000 s)" in text


def test_output_is_valid_replay_input(tmp_path):
    dst = tmp_path / "out.jsonl"
    assert run([str(CSV_FIXTURE), str(dst), *ARGS])[0] == 0
    recs, errors = rp.load_file(dst)
    assert errors == [] and len(recs) == 5
    ordered = rp.order_records(recs)
    assert [r.line for r in ordered] == [1, 3, 2, 4, 5]  # the replay puts the 10:00:01Z row before the +02:00 (10:00:02Z) row


def test_custom_columns_and_extra_columns_ignored(tmp_path):
    src = csv_file(tmp_path, "id,hr,time,note\n7,95,2026-10-20T10:00:00Z,x\n8,,2026-10-20T10:00:01Z,y\n")
    dst = tmp_path / "o.jsonl"
    code, text = run([str(src), str(dst), "--timestamp-column", "time", "--heart-rate-column", "hr", "--athlete-id", "a", "--device-id", "d", "--source", "wearable"])
    assert code == 0, text
    assert [s["sample"]["heart_rate_bpm"] for s in records(dst)] == [95, None]
    assert records(dst)[0]["sample"]["source"] == "wearable"


def test_bom_blank_lines_and_crlf_are_handled(tmp_path):
    src = tmp_path / "bom.csv"
    src.write_bytes(b"\xef\xbb\xbftimestamp,heart_rate_bpm\r\n2026-10-20T10:00:00Z,100\r\n\r\n2026-10-20T10:00:01Z,101\r\n")
    dst = tmp_path / "o.jsonl"
    assert run([str(src), str(dst), *ARGS])[0] == 0
    assert [s["sample"]["heart_rate_bpm"] for s in records(dst)] == [100, 101]


# ---- errors: whole file validated, nothing written ---------------------------------------------------------------

def test_errors_reported_with_row_numbers_and_fields_and_no_output(tmp_path):
    src = csv_file(tmp_path, "\n".join([
        "timestamp,heart_rate_bpm",
        "2026-10-20T10:00:00Z,100",            # row 2 valid
        "2026-10-20T10:00:01,101",             # row 3 naive
        "1760954401,102",                      # row 4 epoch
        "2026-13-01T10:00:00Z,103",            # row 5 invalid date
        "2026-10-20T10:00:05Z,abc",            # row 6 not a number
        "2026-10-20T10:00:06Z,500",            # row 7 out of range (model)
        "2026-10-20T10:00:07Z,inf",            # row 8 not finite
        "2026-10-20T10:00:08Z,104,extra",      # row 9 wrong field count
        "2026-10-20 10:00:09Z,105",            # row 10 space separator: not accepted
        ",106",                                # row 11 empty timestamp
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
        "row 6, heart_rate_bpm: 'abc' is not a number (heart rate must already be in bpm)",
        "row 7, heart_rate_bpm: Input should be less than or equal to 260",
        "row 8, heart_rate_bpm: 'inf' is not a finite number",
        "row 9, row: has 3 fields, header has 2",
        "row 10, timestamp: must be ISO 8601",
        "row 11, timestamp: timestamp is empty",
    ]:
        assert needle in text, needle
    assert "9 error(s) on 9 row(s); 1 row(s) were valid." in text


def test_zero_heart_rate_is_rejected_not_treated_as_missing(tmp_path):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm\n2026-10-20T10:00:00Z,0\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *ARGS])
    assert code == 1 and "row 2, heart_rate_bpm: Input should be greater than or equal to 20" in text


@pytest.mark.parametrize("header,expect", [
    ("time,heart_rate_bpm", "row 1, timestamp: column not found in header ['time', 'heart_rate_bpm']"),
    ("timestamp,hr", "row 1, heart_rate_bpm: column not found"),
    ("timestamp,heart_rate_bpm,heart_rate_bpm", "row 1, heart_rate_bpm: column appears 2 times in the header; the mapping is ambiguous"),
    ("timestamp,timestamp,heart_rate_bpm", "row 1, timestamp: column appears 2 times"),
    (",", "row 1, row: header row is empty"),
])
def test_missing_or_ambiguous_headers(tmp_path, header, expect):
    src = csv_file(tmp_path, header + "\n2026-10-20T10:00:00Z,100,100\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), *ARGS])
    assert code == 1 and expect in text and not (tmp_path / "o.jsonl").exists()


def test_same_column_for_both_is_rejected(tmp_path):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm\n2026-10-20T10:00:00Z,100\n")
    code, text = run([str(src), str(tmp_path / "o.jsonl"), "--timestamp-column", "timestamp", "--heart-rate-column", "timestamp", *ARGS])
    assert code == 1 and "they must differ" in text


def test_empty_file_and_header_only(tmp_path):
    assert "file is empty" in run([str(csv_file(tmp_path, "")), str(tmp_path / "a.jsonl"), *ARGS])[1]
    assert "header but no data rows" in run([str(csv_file(tmp_path, "timestamp,heart_rate_bpm\n")), str(tmp_path / "b.jsonl"), *ARGS])[1]
    assert "Cannot read" in run([str(tmp_path / "missing.csv"), str(tmp_path / "c.jsonl"), *ARGS])[1]


def test_source_must_be_an_existing_category_and_ids_required(tmp_path, capsys):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm\n2026-10-20T10:00:00Z,100\n")
    with pytest.raises(SystemExit) as exc:
        cv.main([str(src), str(tmp_path / "o.jsonl"), "--athlete-id", "a", "--device-id", "d", "--source", "polar"])
    assert exc.value.code == 2 and "invalid choice: 'polar'" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cv.main([str(src), str(tmp_path / "o.jsonl"), "--source", "simulated"])  # athlete-id and device-id are required
    assert sorted(cv.SOURCES) == ["camera", "manual", "simulated", "wearable"]


# ---- timezone handling -----------------------------------------------------------------------------------------------

def test_offsets_are_preserved_textually_and_compared_as_instants(tmp_path):
    src = csv_file(tmp_path, "timestamp,heart_rate_bpm\n2026-10-20T12:00:00+02:00,100\n2026-10-20T10:00:00Z,101\n2026-10-20T05:00:00.250-05:00,102\n")
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
    assert "Dry run: the CSV is valid; no file was written." in text and "5 physiological records" in text
    bad = csv_file(tmp_path, "timestamp,heart_rate_bpm\n2026-10-20T10:00:00,100\n")
    code, text = run([str(bad), str(tmp_path / "p.jsonl"), *ARGS, "--dry-run"])
    assert code == 1 and "row 2, timestamp" in text


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
    source = inspect.getsource(cv)
    assert "httpx" not in source and "urllib" not in source and "socket" not in source


# ---- round trip: converter -> replay -> app -> recorded review -----------------------------------------------------------

class Borrowed:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def test_round_trip_through_replay_and_recorded_review(client, tmp_path):
    dst = tmp_path / "hr.jsonl"
    assert run([str(CSV_FIXTURE), str(dst), *ARGS])[0] == 0
    code = rp.main([str(dst)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=lambda s: None)
    assert code == 0
    hist = client.get("/api/v1/athletes/athlete-1/physiological/history?limit=10").json()
    assert [s["heart_rate_bpm"] for s in hist] == [112, None, 115, 118, 120.5]          # chronological in the store
    assert [s["timestamp"] for s in hist] == ["2026-10-20T10:00:00Z", "2026-10-20T10:00:01Z", "2026-10-20T12:00:02+02:00", "2026-10-20T05:00:03-05:00", "2026-10-20T10:00:04Z"]
    assert all(s["device_id"] == "fictional-hr-csv-1" and s["source"] == "simulated" for s in hist)
    latest = client.get("/api/v1/athletes/athlete-1/physiological/latest").json()
    assert latest["timestamp"] == "2026-10-20T10:00:04Z" and latest["heart_rate_bpm"] == 120.5
    s = client.get("/api/v1/athletes/athlete-1/summary?seconds=60&end=2026-10-20T10:00:04Z").json()["physiological"]
    assert s["sample_count"] == 5 and s["heart_rate_count"] == 4
    assert s["heart_rate_mean_bpm"] == pytest.approx((112 + 115 + 118 + 120.5) / 4) and s["heart_rate_peak_bpm"] == 120.5
    assert s["latest_sample_age_seconds"] == 0.0 and s["max_inter_sample_gap_seconds"] == 1.0
    assert datetime.fromisoformat(s["first_sample_at"]) == datetime(2026, 10, 20, 10, 0, 0, tzinfo=UTC)
    assert client.get("/api/v1/athletes/athlete-1/summary?seconds=60").json()["physiological"]["sample_count"] == 0  # historical, not live
