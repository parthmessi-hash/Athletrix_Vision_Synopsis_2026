"""Vendor-independent wearable foundation (wearable/, scripts/wearable_tool.py) with two FICTIONAL provider formats.

Covers field/unit mapping, nulls, zeros, invalid values, unmapped measurements, duplicates, delayed and out-of-order
records, disconnects, reassignment, clock mapping, provenance, the device-status history, export and a paced replay
into an isolated engine, and the deliberately unimplemented Zephyr boundary. All data is fictional."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wearable import assignments as asg
from wearable.clock import ClockMapping
from wearable.mapping import FieldMap, Mapping, apply
from wearable.normalize import normalize
from wearable.providers.fictional import STRAP, VEST
from wearable.providers.zephyr import ZephyrOmniSenseReader

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "scripts" / "examples" / "wearable"
spec = importlib.util.spec_from_file_location("wearable_tool_cli", ROOT / "scripts" / "wearable_tool.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
ASSIGN = asg.load(EX / "device_assignments_example.json")


def strap():
    return normalize(STRAP.reader(EX / "fictionstrap_example.csv").read(), STRAP, ASSIGN, ClockMapping())


def vest():
    return normalize(VEST.reader(EX / "imaginaryvest_example.jsonl").read(), VEST, ASSIGN, ClockMapping("device_offset", offset_s=-2.5, uncertainty_s=0.2))


def test_strap_nulls_zeros_invalid_values_duplicates_and_contact():
    r = strap()
    by_ts = {s["timestamp"]: s for s in r.samples}
    assert by_ts["2026-10-30T10:00:02Z"] == {"athlete_id": "fictional-athlete-01", "timestamp": "2026-10-30T10:00:02Z", "source": "wearable", "device_id": "strap-07", "hrv_rmssd_ms": 40.1}, "a blank heart rate stays null"
    zero = by_ts["2026-10-30T10:00:03Z"]
    assert "heart_rate_bpm" not in zero and zero["hrv_rmssd_ms"] == 0.0, "an invalid 0 bpm is refused (null, with an issue); a valid zero is kept"
    assert any(i["field"] == "heart_rate_bpm" and i["raw"] == "0" for i in r.issues)
    assert r.skipped == {"duplicate": 1, "conflicting_duplicate": 1, "not_worn": 1, "unassigned": 1}
    assert r.conflicts[0]["kept"]["heart_rate_bpm"] == 131 and r.conflicts[0]["dropped"]["heart_rate_bpm"] == 133, "the first value is kept and both are recorded"
    assert "2026-10-30T10:00:06Z" not in by_ts, "no sample while the strap reports no contact"


def test_disconnects_create_no_samples_and_reassignment_follows_the_observation_time():
    r = strap()
    times = [s["timestamp"] for s in r.samples]
    assert times == sorted(times) and r.ordering["records_out_of_order"] == 1, "emitted in observation order; the late row is counted"
    assert not any("10:00:0" in t[11:19] and t[17:19] in ("08", "09") or "10:00:1" in t for t in times), "nothing between the disconnect (10:00:07) and reconnect (10:00:20)"
    events = [(s["event"], s["timestamp"]) for s in r.status_history if s["event"] != "status_in_data"]
    assert events == [("connected", "2026-10-30T10:00:00Z"), ("disconnected", "2026-10-30T10:00:07Z"), ("connected", "2026-10-30T10:00:20Z")]
    who = {s["timestamp"][11:19]: s["athlete_id"] for s in r.samples}
    assert who["10:00:21"] == "fictional-athlete-01" and who["10:00:31"] == "fictional-athlete-02", "the strap moved to another athlete at 10:00:30"
    assert all(s["device_id"] != "strap-09" for s in r.samples), "an unassigned device never produces a sample"


def test_vest_units_categories_unmapped_fields_clock_offset_and_delays():
    r = vest()
    first = r.samples[0]
    assert first["timestamp"] == "2026-10-30T10:00:01Z", "device clock 2.5 s ahead, corrected by the stated offset"
    assert first["heart_rate_bpm"] == 150.0 and first["skin_temperature_c"] == 34.0 and first["posture"] == "standing" and first["signal_quality"] == 0.92
    assert "core_est_c" not in json.dumps(r.samples) and r.provenance[0]["unmapped_present"] == {"core_est_c": 38.1, "act_g": 0.6}, "estimated core temperature and g-activity stay unmapped"
    assert "activity_level" not in first and "hrv_rmssd_ms" not in first
    second = r.samples[1]
    assert second["breathing_rate_bpm"] == 0.0 and "posture" not in second, "breathing 0 is kept; an unknown posture code is null with an issue"
    reasons = {(i["field"], i["reason"]) for i in r.issues}
    assert ("posture", "not in the category table") in reasons and ("skin_temperature_c", "outside the model's valid range after conversion") in reasons and ("heart_rate_bpm", "not a finite number") in reasons
    assert r.ordering == {"records_out_of_order": 1, "largest_receipt_delay_s": 3.9, "note": r.ordering["note"]}
    assert r.skipped == {"data_while_marked_disconnected": 1}
    prov = r.provenance[0]
    assert prov["clock"] == {"basis": "device_offset", "uncertainty_s": 0.2} and prov["received_at"] == "2026-10-30T10:00:01.250000Z" and prov["timestamp"] == "2026-10-30T10:00:01.000000Z" and prov["observed_raw"] == 1793354403500
    assert prov["mapped_from"]["heart_rate_bpm"] == {"source": "hr_bps", "raw": 2.5, "conversion": "per_second_to_per_minute"}
    battery = [s["battery_percent"] for s in r.status_history if s["event"] == "status_in_data"]
    assert battery == [74, 74, 73, 73, 72], "a battery history the engine itself does not keep"


def test_mappings_refuse_invalid_definitions_and_never_clamp():
    bad = Mapping("x", "1", (FieldMap("heart_rate_bpm", "a"), FieldMap("heart_rate_bpm", "b"), FieldMap("core_temp", "c"), FieldMap("spo2_percent", "d", "furlongs")))
    errors = bad.validate()
    assert any("once" in e for e in errors) and any("core_temp" in e for e in errors) and any("furlongs" in e for e in errors)
    values, issues = apply(Mapping("x", "1", (FieldMap("spo2_percent", "s"),)), {"s": 101})
    assert values == {} and issues[0]["reason"].startswith("outside"), "101 % is refused, not clamped to 100"


def test_clock_mapping_and_assignment_files_are_validated():
    assert ClockMapping("guess").validate() and ClockMapping(offset_s=3).validate() and ClockMapping(uncertainty_s=-1).validate()
    with pytest.raises(ValueError):
        ClockMapping("receipt_minus", latency_s=0.2).to_utc(None, None)
    assert ClockMapping("receipt_minus", latency_s=0.5).to_utc(None, datetime(2026, 10, 30, 10, 0, 1, tzinfo=UTC)) == datetime(2026, 10, 30, 10, 0, 0, 500000, tzinfo=UTC)
    with pytest.raises(asg.AssignmentError, match="overlapping"):
        asg.parse({"format": "athletrix-device-assignments", "format_version": 1, "assignments": [
            {"device": "d", "athlete_id": "a", "from": "2026-10-30T10:00:00Z"}, {"device": "d", "athlete_id": "b", "from": "2026-10-30T11:00:00Z"}]})


def test_cli_export_writes_samples_provenance_and_status_history_and_replays_into_an_isolated_engine(client, tmp_path, monkeypatch):
    from app.core import clock as engine_clock
    monkeypatch.setattr(engine_clock, "utc_now", lambda: datetime(2026, 10, 30, 11, 0, 0, tzinfo=UTC))   # after the fixture's times
    lines = []
    args = ["export", str(EX / "imaginaryvest_example.jsonl"), "--provider", "imaginaryvest-jsonl", "--assignments", str(EX / "device_assignments_example.json"),
            "--clock-basis", "device_offset", "--offset-s", "-2.5", "--uncertainty-s", "0.2", "--out", str(tmp_path / "s.jsonl"), "--provenance", str(tmp_path / "p.jsonl"),
            "--status-log", str(tmp_path / "st.jsonl")]
    assert cli.main(args, out=lines.append) == 0, lines
    recs = [json.loads(x) for x in (tmp_path / "s.jsonl").read_text().splitlines()]
    assert len(recs) == 5 and all(r["type"] == "physiological" for r in recs)
    assert len((tmp_path / "p.jsonl").read_text().splitlines()) == 5 and len((tmp_path / "st.jsonl").read_text().splitlines()) == 7
    assert cli.main(args, out=lines.append) == 1, "never overwrites"
    assert client.post("/api/v1/athletes", json={"id": "fictional-athlete-03", "name": "Fictional Vest Wearer", "jersey_number": 3, "sport": "soccer", "team_id": "t"}).status_code == 201
    replay = ["replay", str(EX / "imaginaryvest_example.jsonl"), "--provider", "imaginaryvest-jsonl", "--assignments", str(EX / "device_assignments_example.json"),
              "--clock-basis", "device_offset", "--offset-s", "-2.5", "--speed", "1000", "--url", "http://testserver"]
    lines = []
    from contextlib import nullcontext
    assert cli.main(replay, out=lines.append, make_client=lambda url: nullcontext(client)) == 0, lines
    assert any(l.startswith("PACED REPLAY") for l in lines)
    hist = client.get("/api/v1/athletes/fictional-athlete-03/physiological/history?limit=10").json()
    assert [h["heart_rate_bpm"] for h in hist] == [150.0, 153.0, 154.8, 156.0, None] and hist[-1]["breathing_rate_bpm"] == 30.0


def test_cli_refuses_missing_assignments_and_reports_format_problems(tmp_path):
    lines = []
    assert cli.main(["validate", str(EX / "fictionstrap_example.csv"), "--provider", "fictionstrap-csv"], out=lines.append) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("time,hr\n2026-10-30T10:00:00Z,120\n")
    assert cli.main(["inspect", str(bad), "--provider", "fictionstrap-csv"], out=lines.append) == 1 and "header must be exactly" in "\n".join(lines)


def test_the_zephyr_boundary_refuses_to_run_and_names_what_is_missing():
    with pytest.raises(NotImplementedError, match="sample payloads"):
        ZephyrOmniSenseReader()
