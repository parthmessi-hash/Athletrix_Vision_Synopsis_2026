"""Camera adapter core (no OpenCV needed): observation contract, calibration, causal estimator, pipeline."""

import json
import math
from datetime import UTC, datetime

import pytest

from app.models import TrackingSample
from camera_adapter import calibration as cal
from camera_adapter.estimator import AthleteEstimator, EstimatorConfig, Point
from camera_adapter.observations import Assignment, Observation, ObservationError, ObservationFile, Video, apply_assignments, parse
from camera_adapter.pipeline import AdapterConfig, TimeMapping, process, records

# a fixed "true" field -> image mapping for the tests (a mild perspective), and its inverse for making observations
H_FIELD_TO_IMAGE = [[18.0, 4.0, 120.0], [0.5, -9.0, 420.0], [0.0, 0.004, 1.0]]


def project(x, y):
    u, v, _ = cal.apply_h(H_FIELD_TO_IMAGE, x, y)
    return [u, v]


FIELD = [(0, 0), (40, 0), (40, 20), (0, 20), (20, 10), (10, 15)]
CHECK = [(30, 5), (5, 8)]


def points(fs):
    return [{"label": f"p{i}", "image": project(*f), "field": list(f)} for i, f in enumerate(fs)]


@pytest.fixture
def calibration():
    return cal.fit(960, 540, points(FIELD), points(CHECK), camera_id="test-cam")


def header(**video):
    return json.dumps({"format": "athletrix-camera-observations", "format_version": 1, "video": {"width": 960, "height": 540, "fps": 10.0, "name": "t.avi", **video},
                       "time_base": "video_relative", "extractor": "test"})


def obs_line(frame, track="trk-1", u=100.0, v=200.0, conf=0.9, athlete=None, t=None):
    return json.dumps({"frame": frame, "video_time_s": frame / 10 if t is None else t, "track_id": track, "u_px": u, "v_px": v, "confidence": conf, "athlete_id": athlete})


# ---- milestone 1: the observation contract ------------------------------------------------------------------------------

def test_valid_observations_parse_and_invalid_ones_are_reported_per_line():
    f = parse([header(), obs_line(0), obs_line(1, athlete="ath-1"), obs_line(1, track="trk-2")])
    assert len(f.observations) == 3 and f.observations[1].athlete_id == "ath-1" and f.video.fps == 10.0
    bad = [header(), obs_line(0, u=2000), obs_line(1, t=5.0), obs_line(2, conf=1.5), obs_line(3), obs_line(3), obs_line(2, track="trk-1"),
           json.dumps({"frame": 4, "video_time_s": 0.4, "track_id": "x", "u_px": 1, "v_px": 1, "confidence": 1, "athlete_id": None, "extra": 1}),
           obs_line(5, track="ath-9", athlete="ath-9")]
    with pytest.raises(ObservationError) as exc:
        parse(bad)
    text = " | ".join(exc.value.errors)
    for needle in ("outside the 960x540 image", "does not equal frame / fps", "confidence must be", "two observations in frame 3", "frames of track 'trk-1' must increase", "exactly the keys", "track ids must differ from athlete ids"):
        assert needle in text, needle
    with pytest.raises(ObservationError, match="video_relative"):
        parse([json.dumps({"format": "athletrix-camera-observations", "format_version": 1, "video": {"width": 9, "height": 9, "fps": 1.0, "name": ""}, "time_base": "capture_utc", "extractor": "x"})])


def test_assignments_are_explicit_and_never_overwrite():
    f = parse([header(), obs_line(0), obs_line(5), obs_line(6, track="trk-2")])
    out = apply_assignments(f, [Assignment("trk-1", "ath-1", from_frame=5)])
    assert [o.athlete_id for o in out.observations] == [None, "ath-1", None], "before from_frame and other tracks stay unassigned"
    pre = parse([header(), obs_line(0, athlete="ath-2")])
    with pytest.raises(ObservationError, match="already assigned"):
        apply_assignments(pre, [Assignment("trk-1", "ath-1")])


# ---- milestone 2: calibration ----------------------------------------------------------------------------------------------

def test_calibration_recovers_a_known_mapping_and_reports_check_points_separately(calibration, tmp_path):
    for f in [(12.5, 3.25), (37.0, 18.0), (0.5, 19.5)]:
        x, y = calibration.to_field(*project(*f))
        assert math.dist((x, y), f) < 1e-6
    assert calibration.fit_residual_m["rms"] < 1e-6 and "not independent" in calibration.fit_residual_m["note"]
    assert calibration.check_errors_m["max"] < 1e-6 and len(calibration.check_errors_m["per_point"]) == 2
    path = tmp_path / "cal.json"
    calibration.save(path)
    doc = json.loads(path.read_text())
    assert doc["format_version"] == 1 and doc["image"] == {"width": 960, "height": 540} and any("fixed camera" in a for a in doc["assumptions"])
    assert cal.load(path).to_field(*project(20, 10)) is not None
    doc["image_to_field"][0][0] += 0.01
    path.write_text(json.dumps(doc))
    with pytest.raises(cal.CalibrationError, match="does not match its fitting points"):
        cal.load(path)
    noisy = [{**p, "image": [p["image"][0] + 0.7, p["image"][1] - 0.4]} if i == 2 else p for i, p in enumerate(points(FIELD))]
    c2 = cal.fit(960, 540, noisy, points(CHECK))
    assert c2.fit_residual_m["rms"] > 0 and c2.check_errors_m["rms"] > 0, "measurement error shows up as residual and check error"


def test_calibration_rejects_insufficient_degenerate_and_inconsistent_points():
    def err(fit_pts, check=None, w=960, h=540):
        with pytest.raises(cal.CalibrationError) as exc:
            cal.fit(w, h, fit_pts, check or [])
        return " | ".join(exc.value.errors)
    assert "at least 4" in err(points(FIELD[:3]))
    assert "degenerate" in err(points([(0, 0), (10, 0), (20, 0), (30, 0), (40, 0)]))
    assert "coincide" in err(points(FIELD[:4]) + [{"label": "dup", "image": project(0, 0), "field": [0.001, 0.0]}])
    assert "finite" in err(points(FIELD[:3]) + [{"image": [math.nan, 1], "field": [1, 1]}])
    assert "outside" in err([{**p, "image": [p["image"][0] + 5000, p["image"][1]]} if i == 0 else p for i, p in enumerate(points(FIELD))])
    assert "must not repeat fitting points" in err(points(FIELD), points([FIELD[0]]))
    assert "positive integers" in err(points(FIELD), w=0)


# ---- milestone 3: the causal estimator ------------------------------------------------------------------------------------

def run(track, cfg=EstimatorConfig(), track_ids=None):
    est = AthleteEstimator(cfg)
    out, resets = [], []
    for i, (t, x, y) in enumerate(track):
        r = est.add(Point(t, x, y, 0.9, (track_ids or {}).get(i, "trk")))
        if r:
            resets.append((t, r))
        e, why = est.estimate()
        out.append((t, e, why))
    return out, resets


def test_estimator_is_exact_for_constant_speed_acceleration_and_rest_and_never_fabricates_warm_up():
    ts = [i / 20 for i in range(60)]
    out, _ = run([(t, 3 + 2.0 * t, 1.0) for t in ts])
    warm = [o for o in out if o[1] is None]
    assert all(why == "warm_up" for _, _, why in warm) and len(warm) == 6, "no estimate before 0.3 s and five points"
    good = [e for _, e, _ in out if e]
    assert all(abs(e.speed_mps - 2.0) < 1e-9 and abs(e.acceleration_mps2) < 1e-6 for e in good)
    out, _ = run([(t, 0.5 * 1.5 * t * t, 0.0) for t in ts])
    good = [e for _, e, _ in out if e]
    assert all(abs(e.acceleration_mps2 - 1.5) < 1e-6 and abs(e.speed_mps - 1.5 * e.t) < 1e-6 for e in good)
    out, _ = run([(t, 8 * t - t * t, 0.0) for t in ts])     # velocity 8 - 2t: slowing down
    good = [e for _, e, _ in out if e]
    assert all(abs(e.acceleration_mps2 + 2.0) < 1e-6 for e in good), "slowing down gives negative acceleration"
    out, _ = run([(t, 10 - t * t, 0.0) for t in ts])         # speeding up while moving towards -x: positive
    assert all(e.acceleration_mps2 > 0 for _, e, _ in out if e and e.speed_mps > 0.3)
    stop = [(t, 4 * t - t * t if t < 2 else 4.0, 0.0) for t in ts]   # decelerating 2 m/s2 to a standstill at 2 s
    out, _ = run(stop)
    slow = [e for _, e, _ in out if e and 0 < e.speed_mps < 0.3 and e.t < 2]
    assert slow and all(e.acceleration_mps2 < 0 for e in slow), "below the direction speed, a stop still reads as negative"
    start = [(t, 0.0 if t < 1 else (t - 1) ** 2, 0.0) for t in ts]
    out, _ = run(start)
    assert all(e.acceleration_mps2 >= 0 for _, e, _ in out if e and e.t > 1 and e.speed_mps < 0.3), "starting off reads as positive"
    out, _ = run([(t, 7.0, 7.0) for t in ts])
    assert all(0 <= e.speed_mps < 1e-9 for _, e, _ in out if e), "standing still is a legitimate (rounding-level) zero, published as 0.0"


def test_estimator_is_causal_resets_on_gaps_and_track_changes():
    track = [(i / 20, i / 20 * 2.0, 0.0) for i in range(40)]
    full, _ = run(track)
    prefix, _ = run(track[:25])
    assert [(t, e) for t, e, _ in prefix] == [(t, e) for t, e, _ in full[:25]], "later observations never change an earlier estimate"
    gapped = track[:20] + track[28:]          # 0.45 s without observations
    out, resets = run(gapped)
    assert resets == [(1.4, "gap")]
    after = [why for t, e, why in out if t >= 1.4][:5]
    assert after == ["warm_up"] * 5, "after a gap the estimator warms up again instead of bridging"
    _, resets = run(track, track_ids={20: "other"} | {i: "other" for i in range(21, 40)})
    assert resets == [(1.0, "track_change")]


# ---- milestone 3: pipeline -----------------------------------------------------------------------------------------------

def make_file(frames, fps=20.0, tracks=(("trk-1", "ath-1", lambda t: (5 + 2 * t, 5.0)),)):
    obs = []
    for f in range(frames):
        t = f / fps
        for tid, athlete, pos in tracks:
            p = pos(t)
            if p is None:
                continue
            u, v = project(*p)
            obs.append(Observation(f, t, tid, u, v, 0.9, athlete))
    return ObservationFile(Video(960, 540, fps, "t"), "test", obs)


def test_pipeline_outputs_at_most_10_hz_never_for_unassigned_tracks_and_validates_samples(calibration):
    f = make_file(40, tracks=(("trk-1", "ath-1", lambda t: (5 + 2 * t, 5.0)), ("trk-2", None, lambda t: (10.0, 10.0))))
    res = process(f, calibration, AdapterConfig())
    assert {o.athlete_id for o in res.outputs} == {"ath-1"} and res.unassigned["trk-2"][2] == 40
    times = [o.video_time_s for o in res.outputs]
    assert all(b - a >= 0.1 - 1e-9 for a, b in zip(times, times[1:])) and len(times) >= 15, "10 Hz from 20 fps, no upsampling"
    mapping = TimeMapping(datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC))
    recs = records(res, mapping, AdapterConfig())
    s = [TrackingSample.model_validate(r["sample"]) for r in recs]
    assert len({x.timestamp for x in s}) == len(s) and s[1].timestamp.microsecond != 0 or s[0].timestamp.microsecond != 0, "sub-second timestamps kept"
    assert all(abs(x.speed_mps - 2.0) < 1e-3 and x.source == "simulated" and x.device_id == "synthetic-marker-cam-1" for x in s)
    assert recs[0]["sample"]["timestamp"] == "2026-10-29T12:00:00." + f"{int(round(res.outputs[0].video_time_s * 1e6)) % 1000000:06d}Z"


def test_pipeline_suppresses_conflicts_low_confidence_outside_and_refuses_mismatched_calibration(calibration):
    f = make_file(30, tracks=(("trk-1", "ath-1", lambda t: (5 + t, 5.0)), ("trk-2", "ath-1", lambda t: (15.0, 5.0) if t < 0.5 else None)))
    res = process(f, calibration, AdapterConfig())
    assert res.skipped["assignment_conflict"] == 20 and all(o.video_time_s >= 0.5 for o in res.outputs), "never one athlete from two tracks"
    low = ObservationFile(f.video, "t", [Observation(o.frame, o.video_time_s, o.track_id, o.u_px, o.v_px, 0.1, o.athlete_id) for o in make_file(20).observations])
    assert process(low, calibration, AdapterConfig()).skipped == {"low_confidence": 20}
    far = make_file(20, tracks=(("trk-1", "ath-1", lambda t: (60.0, 10.0)),))
    assert process(far, calibration, AdapterConfig()).skipped == {"outside_calibration": 20}
    wrong = ObservationFile(Video(1280, 720, 20.0, "x"), "t", [])
    with pytest.raises(ValueError, match="nothing was produced"):
        process(wrong, calibration, AdapterConfig())
    with pytest.raises(ValueError, match="source must be"):
        process(make_file(5), calibration, AdapterConfig(source="wearable"))


# ---- command-line tools ----------------------------------------------------------------------------------------------------

def load_cli(name):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(f"{name}_cli", Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_cli_calibrate_export_and_send_with_an_explicit_anchor(tmp_path):
    import httpx
    from pathlib import Path
    ex = Path(__file__).resolve().parents[1] / "scripts" / "examples" / "camera"
    calibrate, adapter = load_cli("camera_calibrate"), load_cli("camera_to_samples")
    lines = []
    assert calibrate.main(["fit", str(ex / "calibration_points_example.json"), "--out", str(tmp_path / "cal.json")], out=lines.append) == 0
    assert calibrate.main(["fit", str(ex / "calibration_points_example.json"), "--out", str(tmp_path / "cal.json")], out=lines.append) == 1, "never overwrites"
    common = [str(ex / "observations_example.jsonl"), "--calibration", str(tmp_path / "cal.json"), "--config", str(ex / "adapter_config_example.json"), "--assignments", str(ex / "assignments_example.json")]
    assert adapter.main(["export", *common, "--out", str(tmp_path / "s.jsonl")], out=lines.append) == 2, "no anchor, no export"
    assert adapter.main(["validate", *common], out=lines.append) == 0 and not (tmp_path / "s.jsonl").exists()
    assert adapter.main(["export", *common, "--anchor-utc", "2026-10-20T10:00:00Z", "--out", str(tmp_path / "s.jsonl"), "--log", str(tmp_path / "log.json")], out=lines.append) == 0
    log = json.loads((tmp_path / "log.json").read_text())
    assert log["unassigned_tracks"]["trk-2"]["observations"] == 15 and log["skipped"]["warm_up"] == 4 and log["time_mapping"]["meaning"].startswith("anchor stated")
    assert log["samples"][0] == {"athlete_id": "fictional-athlete-01", "frame": 4, "video_time_s": 0.4, "track_id": "trk-1", "points": 5, "fit_rms_m": log["samples"][0]["fit_rms_m"], "speed_sigma_mps": log["samples"][0]["speed_sigma_mps"]}
    assert json.loads((tmp_path / "s.jsonl").read_text().splitlines()[0])["sample"]["timestamp"] == "2026-10-20T10:00:00.400000Z"

    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200 if len(sent) < 10 else 500, json={})
    fixed = datetime(2026, 10, 28, 9, 0, 0, tzinfo=UTC)
    code = adapter.main(["send", *common, "--anchor-utc", "now", "--speed", "1000"], out=lines.append, now=lambda: fixed,
                        make_client=lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(handler)))
    assert code == 3 and len(sent) == 10 and "9 submitted, 1 failed, 6 not sent" in "\n".join(lines)
    assert sent[0]["timestamp"] == "2026-10-28T09:00:00.000000Z", "live-paced: the first sample is timestamped when sending starts"
    assert {s["athlete_id"] for s in sent} == {"fictional-athlete-01"}
