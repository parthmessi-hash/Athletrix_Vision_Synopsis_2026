"""Recorded-sample replay script: validation, ordering, routing, preservation, pacing and failure reporting.

Requests go through httpx MockTransport handlers (counted, never real) or the app's TestClient on an
isolated temporary database. No real sleeps: scheduling uses a controlled clock.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_samples.py"
EXAMPLE = Path(__file__).resolve().parents[1] / "scripts" / "examples" / "fictional_replay.jsonl"
spec = importlib.util.spec_from_file_location("replay_samples", SCRIPT)
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

PHYS = {"athlete_id": "ath-1", "timestamp": "2026-10-20T10:00:00Z", "source": "simulated", "device_id": "rec-1", "heart_rate_bpm": 120}
TRACK = {"athlete_id": "ath-1", "timestamp": "2026-10-20T10:00:00Z", "x_position": 0.0, "y_position": 0.0, "speed_mps": 1.0, "acceleration_mps2": 0.0, "tracking_confidence": 1.0}


def rec(rtype, **over):
    base = dict(PHYS if rtype == "physiological" else TRACK)
    base.update(over)
    return {"type": rtype, "sample": base}


def write_jsonl(tmp_path, records, name="r.jsonl"):
    p = tmp_path / name
    p.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records), encoding="utf-8")
    return p


class Recorder:
    """MockTransport handler that records requests and returns scripted responses."""

    def __init__(self, responses=None, raise_on=None):
        self.requests = []
        self.responses = list(responses or [])
        self.raise_on = raise_on  # (index, exception) -> raise instead of responding to the request at that index

    def __call__(self, request: httpx.Request) -> httpx.Response:
        idx = len(self.requests)
        self.requests.append((request.url.path, json.loads(request.content)))
        if self.raise_on and idx == self.raise_on[0]:
            raise self.raise_on[1]
        return httpx.Response(self.responses[idx] if idx < len(self.responses) else 200, json={"ok": True})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self), base_url="http://replay.test")


class FakeClock:
    """Monotonic clock that only advances when the replay sleeps."""

    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


class Borrowed:
    """Lets the script use the shared TestClient in a `with` block without closing it afterwards."""

    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return False


def run(argv, handler=None, clock=None):
    out = []
    handler = handler or Recorder()
    clock = clock or FakeClock()
    code = rp.main(argv, make_client=lambda url: handler.client(), clock=clock.clock, sleep=clock.sleep, out=out.append)
    return code, "\n".join(out), handler, clock


# ---- validation before any request ------------------------------------------------------------------

def test_example_file_is_valid_and_describes_both_streams_and_athletes():
    records, errors = rp.load_file(EXAMPLE)
    assert errors == []
    assert len(records) == 8
    assert {r.type for r in records} == {"physiological", "tracking"}
    assert {r.athlete_id for r in records} == {"athlete-1", "athlete-2"}
    assert any(r.payload.get("heart_rate_bpm") is None for r in records if r.type == "physiological")
    assert any(r.payload["speed_mps"] == 0.0 for r in records if r.type == "tracking")
    assert all(r.payload["source"] == "simulated" for r in records if r.type == "physiological")


def test_invalid_records_reported_with_line_numbers_and_nothing_sent(tmp_path):
    p = write_jsonl(tmp_path, [
        rec("physiological"),                                             # 1 valid
        rec("heartbeat"),                                                 # 2 unknown type
        rec("physiological", timestamp="2026-10-20T10:00:00"),            # 3 naive timestamp
        rec("tracking", timestamp="yesterday"),                           # 4 unparseable timestamp
        "{not json",                                                      # 5 bad JSON
        json.dumps({"type": "tracking"}),                                 # 6 missing sample
        json.dumps({**rec("tracking"), "note": "x"}),                     # 7 unexpected key
        rec("physiological", spo2_percent=500, bogus=1),                  # 8 two field errors
        "",                                                               # 9 blank: ignored
        rec("tracking", speed_mps=-1),                                    # 10 negative speed
    ])
    records, errors = rp.load_file(p)
    assert len(records) == 1 and records[0].line == 1
    by_line = {}
    for e in errors:
        by_line.setdefault(e.line, []).append(e.message)
    assert set(by_line) == {2, 3, 4, 5, 6, 7, 8, 10}
    assert "unknown record type 'heartbeat'" in by_line[2][0]
    assert "sample.timestamp" in by_line[3][0] and "timezone" in by_line[3][0].lower()
    assert "sample.timestamp" in by_line[4][0]
    assert "not valid JSON" in by_line[5][0]
    assert "missing key(s): sample" in by_line[6][0]
    assert "unexpected key(s): note" in by_line[7][0]
    assert sorted(m.split(":")[0] for m in by_line[8]) == ["sample.bogus", "sample.spo2_percent"]
    assert "sample.speed_mps" in by_line[10][0]
    code, text, handler, _ = run([str(p)])
    assert code == 1 and handler.requests == []
    assert "nothing was sent" in text and "line 3:" in text and "line 10:" in text


def test_empty_or_missing_file(tmp_path):
    p = write_jsonl(tmp_path, ["", "   "])
    _, errors = rp.load_file(p)
    assert [e.message for e in errors] == ["file contains no records"]
    code, text, handler, _ = run([str(tmp_path / "missing.jsonl")])
    assert code == 1 and "Cannot read" in text and handler.requests == []


# ---- ordering --------------------------------------------------------------------------------------------

def test_chronological_order_with_equal_timestamps_in_input_order_and_offset_equality():
    records = [
        rp.parse_line(1, json.dumps(rec("tracking", timestamp="2026-10-20T10:00:02Z"))),
        rp.parse_line(2, json.dumps(rec("physiological", timestamp="2026-10-20T10:00:00Z"))),
        rp.parse_line(3, json.dumps(rec("tracking", timestamp="2026-10-20T12:00:00+02:00"))),  # same instant as line 2
        rp.parse_line(4, json.dumps(rec("physiological", timestamp="2026-10-20T10:00:01Z"))),
        rp.parse_line(5, json.dumps(rec("tracking", timestamp="2026-10-20T10:00:00Z"))),
    ]
    assert [r.line for r in rp.order_records(records)] == [2, 3, 5, 4, 1]


def test_example_file_out_of_order_record_is_placed_chronologically():
    records, _ = rp.load_file(EXAMPLE)
    ordered = rp.order_records(records)
    ts = [r.timestamp for r in ordered]
    assert ts == sorted(ts)
    assert [r.line for r in ordered] == [1, 2, 3, 4, 6, 5, 7, 8]  # line 6 (10:00:01) moves before line 5 (10:00:02)


# ---- routing and preservation -----------------------------------------------------------------------------

def test_each_record_goes_to_its_endpoint_with_payload_untouched(tmp_path):
    p = write_jsonl(tmp_path, [
        rec("tracking", athlete_id="b", timestamp="2026-10-20T10:00:01Z", speed_mps=0.0, tracking_confidence=0.3),
        rec("physiological", athlete_id="a", heart_rate_bpm=None, source="wearable"),
        rec("physiological", athlete_id="b", timestamp="2026-10-20T12:00:01+02:00", heart_rate_bpm=0),  # hr 0 is below the model's minimum: caught before sending
    ])
    code, text, handler, _ = run([str(p)])
    assert code == 1 and handler.requests == []  # the whole file is validated first
    p = write_jsonl(tmp_path, [
        rec("tracking", athlete_id="b", timestamp="2026-10-20T10:00:01Z", speed_mps=0.0, tracking_confidence=0.3),
        rec("physiological", athlete_id="a", heart_rate_bpm=None, source="wearable"),
        rec("physiological", athlete_id="b", timestamp="2026-10-20T12:00:01+02:00", heart_rate_bpm=77.5),
    ])
    code, text, handler, _ = run([str(p)])
    assert code == 0
    assert [path for path, _ in handler.requests] == ["/api/v1/samples/physiological", "/api/v1/samples/tracking", "/api/v1/samples/physiological"]
    sent = [body for _, body in handler.requests]
    assert sent[0] == {**PHYS, "athlete_id": "a", "heart_rate_bpm": None, "source": "wearable"}      # null kept, source kept
    assert sent[1] == {**TRACK, "athlete_id": "b", "timestamp": "2026-10-20T10:00:01Z", "speed_mps": 0.0, "tracking_confidence": 0.3}  # zero and confidence kept
    assert sent[2]["timestamp"] == "2026-10-20T12:00:01+02:00"                                       # timestamp text kept, not normalised or shifted
    assert "3 submitted, 0 failed, 0 not sent" in text


def test_end_to_end_against_app_preserves_values_and_separates_athletes_and_streams(client):
    """Replay the example file into the isolated test database through the real app, then read it back."""
    code = rp.main([str(EXAMPLE)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=lambda s: None)
    assert code == 0
    a1p = client.get("/api/v1/athletes/athlete-1/physiological/history?limit=10").json()
    a1t = client.get("/api/v1/athletes/athlete-1/tracking/history?limit=10").json()
    a2p = client.get("/api/v1/athletes/athlete-2/physiological/history?limit=10").json()
    a2t = client.get("/api/v1/athletes/athlete-2/tracking/history?limit=10").json()
    assert [s["heart_rate_bpm"] for s in a1p] == [112, 115] and a1p[1]["posture"] == "walking"
    assert [s["speed_mps"] for s in a1t] == [0.0, 1.5, 2.0]                       # chronological, zero kept
    assert [s["tracking_confidence"] for s in a1t] == [0.9, 0.95, 1.0]
    assert [s["heart_rate_bpm"] for s in a2p] == [None, 98] and a2p[0]["signal_quality"] == 0.2
    assert [s["speed_mps"] for s in a2t] == [3.2] and a2t[0]["tracking_confidence"] == 0.4
    assert all(s["source"] == "simulated" for s in a1p + a2p)
    assert all(s["device_id"] == "fictional-recorder-1" for s in a1p) and all(s["device_id"] == "fictional-recorder-2" for s in a2p)
    assert a2t[0]["timestamp"] == "2026-10-20T12:00:01+02:00"                     # recorded offset round-trips
    assert datetime.fromisoformat(a1t[-1]["timestamp"]) == datetime(2026, 10, 20, 10, 0, 2, tzinfo=UTC)  # not rebased to now
    latest = client.get("/api/v1/athletes/athlete-1/tracking/latest").json()
    assert latest["speed_mps"] == 2.0 and latest["timestamp"] == "2026-10-20T10:00:02Z"


# ---- dry run ---------------------------------------------------------------------------------------------

def test_dry_run_describes_and_sends_nothing():
    class NoClient:
        def __call__(self, url):
            raise AssertionError("dry run must not create a client")
    out = []
    code = rp.main([str(EXAMPLE), "--dry-run", "--speed", "4"], make_client=NoClient(), clock=lambda: 0.0, sleep=lambda s: None, out=out.append)
    text = "\n".join(out)
    assert code == 0
    assert text.startswith("RECORDED DATA REPLAY")
    assert "8 records: 4 physiological, 4 tracking" in text
    assert "athlete-1 (2 physiological, 3 tracking), athlete-2 (2 physiological, 1 tracking)" in text
    assert "2026-10-20T10:00:00+00:00 to 2026-10-20T10:00:02+00:00 (span 2.000 s)" in text
    assert "speed x4, scheduled duration 0.500 s" in text
    assert "no requests were sent" in text
    assert "rolling 60-second window" in text  # the example is historical: the note explains why it stays out of the live window


# ---- speed and scheduling -------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "-1", "inf", "-inf", "nan", "fast", ""])
def test_invalid_speed_rejected_before_anything_else(bad, capsys):
    with pytest.raises(SystemExit) as exc:
        rp.main([str(EXAMPLE), "--speed=" + bad], make_client=lambda url: (_ for _ in ()).throw(AssertionError("no client")))
    assert exc.value.code == 2
    assert "invalid speed" in capsys.readouterr().err


@pytest.mark.parametrize("speed,expected_sleeps", [(1.0, [1.0, 2.0]), (2.0, [0.5, 1.0]), (0.5, [2.0, 4.0])])
def test_scheduling_uses_recorded_gaps_divided_by_speed_without_changing_timestamps(tmp_path, speed, expected_sleeps):
    p = write_jsonl(tmp_path, [
        rec("physiological", timestamp="2026-10-20T10:00:00Z"),
        rec("physiological", timestamp="2026-10-20T10:00:03Z"),
        rec("physiological", timestamp="2026-10-20T10:00:01Z"),
    ])
    code, _, handler, clock = run([str(p), "--speed", str(speed)])
    assert code == 0
    assert clock.sleeps == expected_sleeps  # first record immediately, then 1 s and 2 s gaps scaled
    assert [b["timestamp"] for _, b in handler.requests] == ["2026-10-20T10:00:00Z", "2026-10-20T10:00:01Z", "2026-10-20T10:00:03Z"]


def test_no_sleep_when_already_behind_schedule(tmp_path):
    p = write_jsonl(tmp_path, [rec("physiological", timestamp="2026-10-20T10:00:00Z"), rec("physiological", timestamp="2026-10-20T10:00:01Z")])

    class SlowBackendClock(FakeClock):
        def clock(self):
            self.now += 5.0  # each clock read finds 5 s have passed (a slow backend)
            return self.now
    code, _, handler, clock = run([str(p)], clock=SlowBackendClock())
    assert code == 0 and clock.sleeps == [] and len(handler.requests) == 2


def test_equal_timestamps_are_sent_without_waiting(tmp_path):
    p = write_jsonl(tmp_path, [rec("physiological"), rec("tracking")])
    code, _, handler, clock = run([str(p)])
    assert code == 0 and clock.sleeps == [] and [path for path, _ in handler.requests] == [rp.ENDPOINTS["physiological"], rp.ENDPOINTS["tracking"]]


# ---- failure reporting ----------------------------------------------------------------------------------

def five(tmp_path):
    return write_jsonl(tmp_path, [rec("physiological", timestamp=f"2026-10-20T10:00:0{i}Z") for i in range(5)])


def test_http_error_stops_and_counts_accurately(tmp_path):
    handler = Recorder(responses=[200, 200, 422, 200, 200])
    code, text, handler, _ = run([str(five(tmp_path))], handler=handler)
    assert code == 2
    assert len(handler.requests) == 3  # nothing after the failure is posted
    assert "2 submitted, 1 failed, 2 not sent (of 5)" in text
    assert "Stopped at line 3" in text and "HTTP 422" in text


def test_connection_error_stops_and_counts_accurately(tmp_path):
    handler = Recorder(raise_on=(0, httpx.ConnectError("connection refused")))
    code, text, handler, _ = run([str(five(tmp_path))], handler=handler)
    assert code == 2 and len(handler.requests) == 1
    assert "0 submitted, 1 failed, 4 not sent (of 5)" in text
    assert "ConnectError" in text


def test_timeout_is_reported_as_unknown_outcome(tmp_path):
    handler = Recorder(raise_on=(3, httpx.ReadTimeout("timed out")))
    code, text, handler, _ = run([str(five(tmp_path))], handler=handler)
    assert code == 2
    assert "3 submitted, 1 failed, 1 not sent (of 5)" in text
    assert "may or may not have stored" in text


def test_backend_validation_is_not_bypassed(client, tmp_path):
    """A record the script accepts but the backend rejects (too far in the future) is a reported failure, not a skip."""
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    p = write_jsonl(tmp_path, [rec("physiological", timestamp="2026-10-20T10:00:00Z"), rec("physiological", timestamp=future)])
    out = []
    code = rp.main([str(p)], make_client=lambda url: Borrowed(client), clock=lambda: 0.0, sleep=lambda s: None, out=out.append)
    text = "\n".join(out)
    assert code == 2
    assert "Warning: 1 record(s) are more than 5 s ahead" in text
    assert "1 submitted, 1 failed, 0 not sent (of 2)" in text and "HTTP 422" in text
    assert len(client.get("/api/v1/athletes/ath-1/physiological/history").json()) == 1
