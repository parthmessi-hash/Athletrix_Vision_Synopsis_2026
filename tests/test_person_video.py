"""Real-video person tracking input (camera_adapter/person_video.py, scripts/camera_track_video.py).

Needs the optional video packages (requirements-video.txt); skipped without them. The tracker runs for real; the
detector is a fixture that returns known boxes per frame, so these tests check the plumbing (observation format,
temporary ids, unconfirmed tracks, skips, track losses, logs, assignment template, refusals) without any model file.
One test runs the pinned YOLOX model on a drawn fictional frame when models/yolox_s.onnx is present. Fictional data only.
"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
pytest.importorskip("supervision")
pytest.importorskip("trackers")

from camera_adapter import person_video as pv  # noqa: E402
from camera_adapter.observations import load, load_assignments  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("camera_track_video_cli", ROOT / "scripts" / "camera_track_video.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
MODEL = ROOT / "models" / "yolox_s.onnx"
FPS, W, H = 10.0, 320, 240


def write_video(path, frames=60):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    for _ in range(frames):
        vw.write(np.full((H, W, 3), 80, np.uint8))
    vw.release()
    return path


class FixtureDetector:
    """Returns the boxes scripted for each successive frame: {frame: [(x1, y1, x2, y2, score), ...]}."""

    def __init__(self, script):
        self.script, self.f = script, 0

    def __call__(self, frame):
        boxes = self.script(self.f)
        self.f += 1
        return np.array([b[:4] for b in boxes], np.float32).reshape(-1, 4), np.array([b[4] for b in boxes], np.float32)


def walker(f, x0=40.0, y0=100.0, dx=2.0):
    x = x0 + dx * f
    return (x, y0, x + 20, y0 + 50, 0.9)


def test_observations_follow_the_adapter_contract_with_temporary_unassigned_ids(tmp_path):
    video = write_video(tmp_path / "v.avi")
    det = FixtureDetector(lambda f: [walker(f), walker(f, x0=200, y0=60, dx=-1.5)])
    obs, log = pv.track_video(video, pv.VideoTrackingConfig(), detector=det)
    pv_path = tmp_path / "obs.jsonl"
    from camera_adapter.observations import dump  # noqa: PLC0415
    dump(obs, pv_path)
    again = load(pv_path)                     # the adapter's strict validator accepts it
    assert {o.track_id for o in again.observations} == set(log["tracks"]) and len(log["tracks"]) == 2
    assert all(o.athlete_id is None and o.track_id.startswith("trk-") for o in again.observations)
    first = min((o for o in again.observations if o.u_px < 150), key=lambda o: o.frame)
    x1, y1, x2, y2, score = walker(first.frame)
    assert first.video_time_s == round(first.frame / FPS, 6) and first.confidence == pytest.approx(score)
    assert (first.u_px, first.v_px) == (pytest.approx((x1 + x2) / 2, abs=0.01), pytest.approx(y2, abs=0.01))   # bottom-centre ground point
    # unconfirmed frames (minimum_consecutive_frames = 3) produce nothing and are counted
    assert first.frame >= 2 and log["skipped"]["unconfirmed_track"] >= 2
    assert log["video"]["decoded_frames"] == 60 and log["video"]["decoder_time_mismatches"] == 0 and log["video"]["fps"] == FPS
    assert log["model"] is None and log["config"]["tracker"]["implementation"] == "trackers.ByteTrackTracker" and log["versions"]["trackers"]
    assert log["speed"]["frames_per_second"] > 0 and "not camera capture time" in log["video"]["time_base"]


def test_short_losses_keep_the_id_and_are_logged_long_losses_start_a_new_temporary_track(tmp_path):
    video = write_video(tmp_path / "v.avi", frames=150)
    def script(f):
        if 20 <= f < 24 or 60 <= f < 140:    # hidden 4 frames (kept), then 80 frames (longer than the buffer: a new id)
            return []
        return [walker(f % 60 if f < 60 else 30, dx=1.0)]
    obs, log = pv.track_video(video, pv.VideoTrackingConfig(), detector=FixtureDetector(script))
    tracks = log["tracks"]
    assert len(tracks) == 2
    a, b = (tracks[k] for k in sorted(tracks, key=lambda k: int(k.split("-")[1])))
    assert a["losses"] == [{"last_seen_frame": 19, "reappeared_frame": 24, "missing_frames": 4}] and a["last_frame"] == 59
    assert b["first_frame"] >= 140 and log["track_losses"] == 1


def test_boxes_cut_by_the_bottom_edge_or_too_small_are_not_observations(tmp_path):
    video = write_video(tmp_path / "v.avi", frames=20)
    det = FixtureDetector(lambda f: [(10, H - 60, 40, H - 1, 0.9), (200, 50, 205, 58, 0.9), walker(f)])
    obs, log = pv.track_video(video, pv.VideoTrackingConfig(), detector=det)
    assert log["skipped"]["box_touches_bottom_edge"] > 0 and log["skipped"]["box_too_small"] > 0
    assert all(o.v_px < H - 2 and o.u_px > 41 for o in obs.observations)


def test_cli_writes_validated_outputs_a_template_without_assignments_and_previews(tmp_path):
    video = write_video(tmp_path / "v.avi", frames=30)
    lines = []
    args = [str(video), "--out", str(tmp_path / "o.jsonl"), "--log", str(tmp_path / "l.json"), "--assignments-template", str(tmp_path / "a.json"),
            "--gallery", str(tmp_path / "g.png"), "--annotate", str(tmp_path / "r.mp4")]
    assert cli.main(args, out=lines.append, detector=FixtureDetector(lambda f: [walker(f)])) == 0, lines
    tmpl = json.loads((tmp_path / "a.json").read_text())
    assert tmpl["assignments"] == [] and [t["track_id"] for t in tmpl["tracks_to_review"]] == list(json.loads((tmp_path / "l.json").read_text())["tracks"])
    assert load_assignments(tmp_path / "a.json") == []                    # loadable by the adapter as is: nothing assigned
    assert cv2.imread(str(tmp_path / "g.png")) is not None and (tmp_path / "r.mp4").stat().st_size > 0
    assert any("Every observation is unassigned" in x for x in lines)
    # refuses to overwrite anything
    assert cli.main(args, out=lines.append, detector=FixtureDetector(lambda f: [walker(f)])) == 1


def test_cli_needs_a_local_model_and_refuses_an_unexpected_file(tmp_path):
    video = write_video(tmp_path / "v.avi", frames=5)
    lines = []
    assert cli.main([str(video), "--out", str(tmp_path / "o.jsonl"), "--log", str(tmp_path / "l.json")], out=lines.append) == 2
    assert "--model is required" in lines[-1]
    fake = tmp_path / "yolox_s.onnx"
    fake.write_bytes(b"not the model")
    assert cli.main([str(video), "--model", str(fake), "--out", str(tmp_path / "o.jsonl"), "--log", str(tmp_path / "l.json")], out=lines.append) == 1
    assert "refusing to run an unexpected model" in lines[-1] and not (tmp_path / "o.jsonl").exists()
    with pytest.raises(FileNotFoundError):
        pv.verify_model(tmp_path / "missing.onnx", "yolox_s")


def test_configuration_is_explicit_and_validated():
    doc = json.loads((ROOT / "scripts" / "examples" / "camera" / "video_tracking_config_example.json").read_text())
    assert pv.VideoTrackingConfig.from_doc(doc) == pv.VideoTrackingConfig()
    for bad in ({"detector": {"input_size": 100}}, {"tracker": {"implementation": "other"}}, {"ground_point": "head"}):
        with pytest.raises(ValueError):
            pv.VideoTrackingConfig.from_doc({**doc, **bad})


@pytest.mark.skipif(not MODEL.is_file(), reason="models/yolox_s.onnx not fetched (scripts/fetch_person_model.py)")
def test_pinned_detector_finds_drawn_fictional_figures_near_their_feet():
    from camera_adapter import synthetic_people as sp  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    pv.verify_model(MODEL, "yolox_s")
    det = pv.YoloxPersonDetector(MODEL, pv.DetectorConfig())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scene.avi"
        sp.render(path)
        cap = cv2.VideoCapture(str(path))
        cap.set(cv2.CAP_PROP_POS_FRAMES, 250)
        ok, frame = cap.read()
        cap.release()
    assert ok
    boxes, scores = det(frame)
    cam = sp.Camera3()
    feet = [cam.project(*sp.truth(n, 10.0)[0]) for n in sp.FIGURES if sp.in_view(cam, n, 10.0, 20)]
    strong = [(b, s) for b, s in zip(boxes, scores) if s >= 0.5]
    assert len(strong) >= len(feet) - 1
    matched = sum(1 for fu, fv in feet if any(abs((b[0] + b[2]) / 2 - fu) < 15 and abs(b[3] - fv) < 15 for b, _ in strong))
    assert matched >= len(feet) - 1


def test_cli_paced_replay_reports_bounded_processing_and_keeps_video_times(tmp_path):
    video = write_video(tmp_path / "v.avi", frames=20)    # 2 s at 10 fps, released at 4x its rate
    def slow(frame):
        time.sleep(0.04)
        return np.array([walker(0)[:4]], np.float32), np.array([0.9], np.float32)
    lines = []
    args = [str(video), "--out", str(tmp_path / "o.jsonl"), "--log", str(tmp_path / "l.json"), "--paced-replay", "--speed", "4", "--queue-size", "1"]
    assert cli.main(args, out=lines.append, detector=slow) == 0, lines
    log = json.loads((tmp_path / "l.json").read_text())
    b = log["bounded"]
    assert log["mode"] == "bounded_paced_file_replay" and b["frames"]["released_by_source"] == 20
    assert b["frames"]["processed"] + b["frames"]["dropped"] == 20 and b["frames"]["dropped"] > 0 and b["kept_up"] is False
    assert "not a camera" in b["source"] and any(line.startswith("PACED FILE REPLAY") for line in lines)
    obs = load(tmp_path / "o.jsonl").observations
    assert all(o.video_time_s == pytest.approx(o.frame / FPS) for o in obs), "video-relative times, whatever the processing speed"
