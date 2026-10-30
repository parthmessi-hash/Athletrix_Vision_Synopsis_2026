"""Bounded frame processing (camera_adapter/bounded.py): no backlog, every dropped frame accounted for, capture times
preserved, processing lag reported, continuity kept after dropped work. Synthetic frames and deliberately slow fixture
detectors; no camera. The tracker-based test needs the optional video packages."""

import time
from datetime import UTC, datetime

import pytest

from camera_adapter.bounded import BoundedConfig, bounded_log, run_bounded, synthetic_source


def test_a_fast_processor_keeps_up_and_sees_every_frame_at_its_capture_time():
    seen = []
    res = run_bounded(synthetic_source(25, 50.0, lambda f: f), lambda i, t, d: seen.append((i, t, d)), BoundedConfig())
    log = bounded_log(res, BoundedConfig(), 50.0, "synthetic")
    assert [s[0] for s in seen] == list(range(25)) and all(t == pytest.approx(i / 50.0) and d == i for i, t, d in seen)
    assert log["frames"]["dropped"] == 0 and log["kept_up"] is True and log["frames"]["processed"] == 25


def test_a_slow_processor_drops_the_oldest_frames_and_never_builds_a_backlog():
    cfg = BoundedConfig(queue_size=2)
    waiting = []
    res = run_bounded(synthetic_source(60, 50.0, lambda f: f), lambda i, t, d: (waiting.append(i), time.sleep(0.06)), cfg)   # 60 ms per frame at 50 fps
    log = bounded_log(res, cfg, 50.0, "synthetic, deliberately slow processor")
    f = log["frames"]
    assert f["released_by_source"] == 60 and f["processed"] + f["dropped"] == 60 and f["dropped"] > 30, f
    assert log["kept_up"] is False
    assert log["processing_lag_s"]["max"] < 0.06 * (cfg.queue_size + 1) + 0.1, "lag is bounded by the queue, not growing with the run"
    assert res.processed == sorted(res.processed, key=lambda p: p["index"]), "frames are processed in capture order"
    assert all(p["capture_s"] == pytest.approx(p["index"] / 50.0) for p in res.processed), "capture times survive the queue"
    assert set(i for i, _ in res.dropped).isdisjoint(p["index"] for p in res.processed)
    assert res.processed[-1]["index"] >= 55, "the newest frames are the ones processed at the end"


def test_sampling_every_nth_frame_is_counted_apart_from_drops():
    cfg = BoundedConfig(process_every_nth=3)
    res = run_bounded(synthetic_source(30, 100.0, lambda f: f), lambda i, t, d: None, cfg)
    log = bounded_log(res, cfg, 100.0, "synthetic")
    assert log["frames"]["sampled_out"] == 20 and log["frames"]["dropped"] == 0 and [p["index"] for p in res.processed] == list(range(0, 30, 3))


def test_configuration_is_validated():
    assert BoundedConfig(queue_size=0).validate() and BoundedConfig(process_every_nth=0).validate() and BoundedConfig(overflow="block").validate()
    with pytest.raises(ValueError):
        run_bounded([], lambda *a: None, BoundedConfig(queue_size=0))


def test_dropped_work_becomes_a_continuity_break_downstream_and_never_an_invented_sample():
    pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    pytest.importorskip("supervision")
    pytest.importorskip("trackers")
    from app.core.distance import estimate_distance
    from app.models import TrackingSample
    from camera_adapter import calibration as cal
    from camera_adapter.observations import ObservationFile, Video, apply_assignments, Assignment
    from camera_adapter.person_video import FrameTracker, VideoTrackingConfig
    from camera_adapter.pipeline import AdapterConfig, TimeMapping, process, records

    fps = 25.0

    def detector(frame):     # one person walking right; the detector stalls for 0.7 s once
        f = frame
        if f == 30:
            time.sleep(0.7)
        else:
            time.sleep(0.01)
        x = 100 + 4 * f
        return np.array([[x, 200, x + 30, 300]], np.float32), np.array([0.9], np.float32)

    ft = FrameTracker(VideoTrackingConfig(), detector, fps, 960, 540)
    cfg = BoundedConfig(queue_size=1)
    res = run_bounded(synthetic_source(75, fps, lambda f: f), lambda i, t, d: ft.process(i, t, d), cfg)
    log = bounded_log(res, cfg, fps, "synthetic frames, stalling fixture detector")
    assert log["frames"]["dropped"] >= 10 and log["frames"]["longest_drop_run_s"] > 0.5
    obs = sorted(ft.obs, key=lambda o: (o.frame, o.track_id))
    assert all(o.video_time_s == pytest.approx(o.frame / fps) for o in obs), "observation times are capture times"
    track = obs[0].track_id
    assert {o.track_id for o in obs} == {track}, "a 0.7 s gap is inside the tracker's time-based budget: the same temporary track"
    def img(x, y):
        return [100 + 40 * x, 500 - 10 * y]
    pts = [{"label": f"p{i}", "image": img(*f), "field": list(f)} for i, f in enumerate([(0, 0), (20, 0), (20, 20), (0, 20), (10, 10)])]
    calib = cal.fit(960, 540, pts, [], camera_id="test")
    of = apply_assignments(ObservationFile(Video(960, 540, fps, "synthetic"), "test", obs), [Assignment(track, "ath-1")])
    out = process(of, calib, AdapterConfig())
    assert "gap" in [s["started_by"] for s in out.segments], "the dropped work exceeds the estimator's 0.3 s gap rule: a new segment"
    samples = [TrackingSample.model_validate(r["sample"]) for r in records(out, TimeMapping(datetime(2026, 10, 30, tzinfo=UTC)), AdapterConfig(), "t")]
    assert estimate_distance(samples, 3).continuity_break_count >= 1
    assert len(samples) <= len(res.processed), "no sample for a dropped frame"
