"""Real-video person tracking input: a local pretrained person detector + ByteTrack -> camera observations (format v1).

Optional dependencies (requirements-video.txt): OpenCV (video decoding and the ONNX detector through cv2.dnn), NumPy,
and Roboflow `trackers` (ByteTrack, Apache-2.0) with `supervision` (MIT) for its detection container. The engine and
the adapter core never import this module.

DETECTOR  YOLOX (Megvii, Apache-2.0), the official ONNX export, COCO-pretrained; only class 0 ("person") is kept. The
          model file must already be on disk (scripts/fetch_person_model.py fetches it once, explicitly); processing
          never downloads anything and refuses a file whose SHA-256 differs from the pinned one unless the operator
          names it as an unpinned custom model. The detector finds people of any role: players, referees, coaches and
          spectators alike. It does not read jerseys, recognise faces or classify soccer roles.
TRACKER   ByteTrack as implemented by `trackers.ByteTrackTracker`, updated with each frame's video time. Its track ids
          are temporary: "trk-<n>" means "the same box chain as in earlier frames" and nothing about who it is. After
          a loss longer than the tracker's buffer, a crossing or a re-entry a person may get a new id, or (rarely) an
          id may move to another person; both are why assignment stays an explicit, reviewed operator step.
OUTPUT    one observation per tracked box: frame, video_time_s = frame / fps (constant-frame-rate contract; the
          decoder's timestamps are checked against it), track_id, the box's bottom-centre as the ground-contact point
          (u_px, v_px), the detector's score as confidence, athlete_id null. Boxes whose bottom edge touches the image
          border (feet out of view) or that are smaller than the configured height are not turned into observations
          and are counted instead. Unconfirmed tracks (ByteTrack id -1) produce nothing.
GROUND    `ground_contact` is evidence about the ground point, kept apart from the detector's confidence: rule
          "box_height_consistency" compares each box's height with the median of the same track's last
          `ground_reference_frames` consistent heights; a box shorter than `ground_min_height_ratio` times that
          reference is "suspect" (typically legs hidden behind something: the box ends above the feet, so its
          bottom-centre maps too far away, while the detector can stay confident). Until `ground_min_reference`
          consistent heights exist the status is "unknown". Suspect boxes never enter the reference. This is a
          heuristic: it misses occlusions present from a track's first frames and slow ones, and it flags a fast
          crouch; it does not locate the feet.
LOG       model name, file SHA-256 and licence, every configuration value, package versions, the video's properties and
          SHA-256, frames decoded, decoder time mismatches (dropped or duplicated frames), detections, per-track
          first/last frame and the frames where each track was lost and reappeared, skips by reason, and processing
          speed. Video time is video-relative: it is not camera capture time.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from camera_adapter.observations import Observation, ObservationFile, Video

CONFIG_FORMAT = "athletrix-video-tracking-config"

# Pinned model files: fetched once by scripts/fetch_person_model.py, verified before every run. The SHA-256 values are
# the ones observed when the official release assets were first fetched for this project (yolox_s 2026-10-29, yolox_tiny 2026-10-30); the upstream
# release publishes no checksums of its own, so this pin detects a changed file, it does not prove the original.
MODELS = {
    "yolox_s": {"url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx", "bytes": 35858002,
                "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063", "input_size": 640,
                "licence": "Apache-2.0 (Megvii YOLOX repository and release); trained on COCO 2017",
                "source": "Megvii-BaseDetection/YOLOX release 0.1.1rc0, official ONNX export"},
    "yolox_tiny": {"url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx", "bytes": 20219662,
                   "sha256": "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7", "input_size": 416,
                   "licence": "Apache-2.0 (Megvii YOLOX repository and release); trained on COCO 2017",
                   "source": "Megvii-BaseDetection/YOLOX release 0.1.1rc0, official ONNX export"},
}
PERSON_CLASS = 0


@dataclass(frozen=True)
class DetectorConfig:
    model: str = "yolox_s"
    input_size: int = 640
    score_threshold: float = 0.1          # low on purpose: ByteTrack's second stage uses low-score boxes
    nms_iou_threshold: float = 0.45
    backend: str = "opencv-dnn"


@dataclass(frozen=True)
class TrackerConfig:
    implementation: str = "trackers.ByteTrackTracker"
    lost_track_buffer: int = 30            # in 30 fps frames, scaled by the tracker to the video's frame rate
    track_activation_threshold: float = 0.5
    minimum_consecutive_frames: int = 3
    minimum_iou_threshold: float = 0.1
    high_conf_det_threshold: float = 0.5


@dataclass(frozen=True)
class VideoTrackingConfig:
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    ground_point: str = "bbox_bottom_centre"
    drop_boxes_touching_bottom_edge: bool = True
    min_box_height_px: float = 12.0
    max_time_mismatch_frames: float = 0.5   # decoder timestamp vs frame / fps, in frame periods, before a frame counts as mismatched
    ground_contact_rule: str = "box_height_consistency"   # or "none" (no ground_contact written)
    ground_min_height_ratio: float = 0.85
    ground_reference_frames: int = 15
    ground_min_reference: int = 5

    def validate(self) -> list[str]:
        e = []
        d, t = self.detector, self.tracker
        if d.backend != "opencv-dnn":
            e.append("detector.backend must be opencv-dnn (the only backend in version 1)")
        if not (isinstance(d.input_size, int) and 160 <= d.input_size <= 1920 and d.input_size % 32 == 0):
            e.append("detector.input_size must be a multiple of 32 between 160 and 1920")
        if not (0 < d.score_threshold < 1 and 0 < d.nms_iou_threshold < 1):
            e.append("detector.score_threshold and nms_iou_threshold must be in (0, 1)")
        if t.implementation != "trackers.ByteTrackTracker":
            e.append("tracker.implementation must be trackers.ByteTrackTracker")
        if not (isinstance(t.lost_track_buffer, int) and t.lost_track_buffer >= 0 and isinstance(t.minimum_consecutive_frames, int) and t.minimum_consecutive_frames >= 1):
            e.append("tracker.lost_track_buffer must be an integer >= 0 and minimum_consecutive_frames >= 1")
        if not all(0 < v < 1 for v in (t.track_activation_threshold, t.minimum_iou_threshold, t.high_conf_det_threshold)):
            e.append("tracker thresholds must be in (0, 1)")
        if self.ground_point != "bbox_bottom_centre":
            e.append("ground_point must be bbox_bottom_centre (the only rule in version 1)")
        if not (self.min_box_height_px >= 0 and self.max_time_mismatch_frames > 0):
            e.append("min_box_height_px must be >= 0 and max_time_mismatch_frames > 0")
        if self.ground_contact_rule not in ("box_height_consistency", "none"):
            e.append("ground_contact_rule must be box_height_consistency or none")
        if not (0 < self.ground_min_height_ratio < 1 and isinstance(self.ground_reference_frames, int) and isinstance(self.ground_min_reference, int)
                and 1 <= self.ground_min_reference <= self.ground_reference_frames):
            e.append("ground_min_height_ratio must be in (0, 1) and 1 <= ground_min_reference <= ground_reference_frames")
        return e

    def to_doc(self) -> dict:
        return {"format": CONFIG_FORMAT, "format_version": 1, **asdict(self)}

    @classmethod
    def from_doc(cls, doc: dict) -> "VideoTrackingConfig":
        if not isinstance(doc, dict) or doc.get("format") != CONFIG_FORMAT or doc.get("format_version") != 1:
            raise ValueError(f"not an {CONFIG_FORMAT} version 1 document")
        body = {k: v for k, v in doc.items() if k not in ("format", "format_version")}
        cfg = cls(detector=DetectorConfig(**body.pop("detector", {})), tracker=TrackerConfig(**body.pop("tracker", {})), **body)
        errors = cfg.validate()
        if errors:
            raise ValueError("; ".join(errors))
        return cfg


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_model(path: Path, name: str, *, allow_unpinned: bool = False) -> dict:
    """The model's provenance for the log; raises when the file is missing or differs from its pin."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"model file {path} not found; fetch it once with: python scripts/fetch_person_model.py --model {name} (processing never downloads)")
    digest = sha256_file(path)
    known = MODELS.get(name)
    if known is None or known["sha256"] is None:
        if not allow_unpinned:
            raise ValueError(f"model {name!r} has no pinned SHA-256; pass --allow-unpinned-model to use it (its SHA-256 {digest} is then logged)")
        return {"name": name, "file": path.name, "sha256": digest, "pinned": False, **({k: known[k] for k in ("licence", "source")} if known else {})}
    if digest != known["sha256"]:
        raise ValueError(f"model file {path} has SHA-256 {digest}, not the pinned {known['sha256']}; refusing to run an unexpected model")
    return {"name": name, "file": path.name, "sha256": digest, "pinned": True, "licence": known["licence"], "source": known["source"]}


class YoloxPersonDetector:
    """YOLOX ONNX through OpenCV DNN: letterbox to a square input, decode the grid outputs, keep persons, NMS."""

    def __init__(self, model_path: Path, config: DetectorConfig) -> None:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        self._cv2, self._np = cv2, np
        self.config = config
        self.net = cv2.dnn.readNetFromONNX(str(model_path))
        n = config.input_size
        grids, strides = [], []
        for s in (8, 16, 32):
            xv, yv = np.meshgrid(np.arange(n // s), np.arange(n // s))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((grids[-1].shape[0], 1), s))
        self._grid, self._stride = np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)

    def __call__(self, frame) -> tuple:
        """(boxes Nx4 x1,y1,x2,y2 in frame pixels, scores N) for persons."""
        cv2, np = self._cv2, self._np
        n = self.config.input_size
        h, w = frame.shape[:2]
        r = min(n / h, n / w)
        padded = np.full((n, n, 3), 114, dtype=np.uint8)
        padded[: int(h * r), : int(w * r)] = cv2.resize(frame, (int(w * r), int(h * r)), interpolation=cv2.INTER_LINEAR)
        self.net.setInput(padded.transpose(2, 0, 1)[None].astype(np.float32))
        out = self.net.forward()[0]
        if out.shape[0] != self._grid.shape[0]:
            raise ValueError(f"unexpected detector output {out.shape}; is this a YOLOX ONNX export with input {n}?")
        xy = (out[:, :2] + self._grid) * self._stride
        wh = np.exp(out[:, 2:4]) * self._stride
        score = out[:, 4] * out[:, 5 + PERSON_CLASS]
        keep = score >= self.config.score_threshold
        xy, wh, score = xy[keep] / r, wh[keep] / r, score[keep]
        if not len(score):
            return np.empty((0, 4), np.float32), np.empty((0,), np.float32)
        tl = xy - wh / 2
        idx = cv2.dnn.NMSBoxes([[float(a), float(b), float(c), float(d)] for (a, b), (c, d) in zip(tl, wh)], score.astype(float).tolist(),
                               self.config.score_threshold, self.config.nms_iou_threshold)
        idx = np.array(idx, dtype=int).reshape(-1)
        boxes = np.concatenate([tl[idx], tl[idx] + wh[idx]], axis=1)
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w - 1)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h - 1)
        return boxes.astype(np.float32), score[idx].astype(np.float32)


@dataclass
class TrackStats:
    first_frame: int
    last_frame: int
    observations: int = 0
    losses: list = field(default_factory=list)    # [last_seen_frame, reappeared_frame] for each interruption

    def to_doc(self, fps: float) -> dict:
        return {"first_frame": self.first_frame, "last_frame": self.last_frame, "first_video_time_s": round(self.first_frame / fps, 6),
                "last_video_time_s": round(self.last_frame / fps, 6), "observations": self.observations,
                "losses": [{"last_seen_frame": a, "reappeared_frame": b, "missing_frames": b - a - 1} for a, b in self.losses]}


def package_versions() -> dict:
    out = {"python": platform.python_version()}
    for mod in ("cv2", "numpy", "trackers", "supervision"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", None)
        except ImportError:
            out[mod] = None
    if out.get("trackers") is None:
        try:
            from importlib.metadata import version  # noqa: PLC0415
            out["trackers"] = version("trackers")
        except Exception:  # noqa: BLE001
            pass
    return out


class FrameTracker:
    """Detection, tracking, ground-contact evidence and observations for one frame at a time, at the frame's own time.

    Shared by the offline file path (`track_video`, every decoded frame) and the bounded path (`camera_adapter.bounded`,
    which may skip or drop frames): the tracker is updated with each processed frame's time, so a skipped frame is a gap
    in time, not a missing step. `frame_step` is the expected index step between processed frames (sampling every nth
    frame); a larger step is logged as a track loss."""

    def __init__(self, config: VideoTrackingConfig, detector, fps: float, width: int, height: int, *, frame_step: int = 1) -> None:
        import numpy as np  # noqa: PLC0415
        import supervision as sv  # noqa: PLC0415
        from trackers import ByteTrackTracker  # noqa: PLC0415
        errors = config.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self._np, self._sv = np, sv
        self.config, self.detector, self.fps, self.width, self.height, self.frame_step = config, detector, fps, width, height, frame_step
        t = config.tracker
        self.tracker = ByteTrackTracker(lost_track_buffer=t.lost_track_buffer, frame_rate=fps, track_activation_threshold=t.track_activation_threshold,
                                        minimum_consecutive_frames=t.minimum_consecutive_frames, minimum_iou_threshold=t.minimum_iou_threshold,
                                        high_conf_det_threshold=t.high_conf_det_threshold)
        self.obs: list[Observation] = []
        self.tracks: dict[str, TrackStats] = {}
        self.heights: dict[str, list[float]] = {}     # track -> recent consistent box heights (the ground-contact reference)
        self.ground_counts = {"consistent": 0, "suspect": 0, "unknown": 0}
        self.skipped = {"unconfirmed_track": 0, "box_touches_bottom_edge": 0, "box_too_small": 0}
        self.detections_total = self.frames = 0
        self.t_detect = self.t_track = 0.0

    def process(self, f: int, t: float, frame) -> list:
        """Process frame index `f` taken at `t` seconds (video or capture time); returns [(track_id, box, score, used)]."""
        np, sv, config = self._np, self._sv, self.config
        a = time.perf_counter()
        boxes, scores = self.detector(frame)
        b = time.perf_counter()
        boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
        scores = np.asarray(scores, dtype=np.float32).reshape(-1)
        self.detections_total += len(scores)
        det = sv.Detections(xyxy=boxes, confidence=scores, class_id=np.zeros(len(scores), dtype=int))
        tracked = self.tracker.update(det, timestamp=t)
        c = time.perf_counter()
        self.t_detect += b - a
        self.t_track += c - b
        self.frames += 1
        shown = []
        for box, score, tid in zip(tracked.xyxy, tracked.confidence if tracked.confidence is not None else np.ones(len(tracked)), tracked.tracker_id):
            x1, y1, x2, y2 = (float(v) for v in box)
            if tid is None or int(tid) < 0:
                self.skipped["unconfirmed_track"] += 1
                shown.append((None, (x1, y1, x2, y2), float(score), False))
                continue
            track_id = f"trk-{int(tid)}"
            used = True
            if config.drop_boxes_touching_bottom_edge and y2 >= self.height - 1.5:
                self.skipped["box_touches_bottom_edge"] += 1
                used = False
            elif y2 - y1 < config.min_box_height_px:
                self.skipped["box_too_small"] += 1
                used = False
            st = self.tracks.get(track_id)
            if st is None:
                st = self.tracks[track_id] = TrackStats(f, f)
            elif f > st.last_frame + self.frame_step:
                st.losses.append([st.last_frame, f])
            st.last_frame = f
            shown.append((track_id, (x1, y1, x2, y2), float(score), used))
            if used:
                st.observations += 1
                u = min(max((x1 + x2) / 2, -0.5), self.width - 0.5)
                v = min(max(y2, -0.5), self.height - 0.5)
                ground = ground_status(self.heights.setdefault(track_id, []), y2 - y1, config)
                if ground is not None:
                    self.ground_counts[ground] += 1
                self.obs.append(Observation(f, round(t, 6), track_id, round(u, 3), round(v, 3), round(min(1.0, max(0.0, float(score))), 4), None, ground))
        return shown

    def summary(self) -> dict:
        """The tracking part of a run log (detections, observations, skips, ground evidence, tracks, per-frame costs)."""
        n = max(1, self.frames)
        return {"detections": self.detections_total, "observations": len(self.obs), "skipped": self.skipped,
                "ground_contact": {"rule": self.config.ground_contact_rule, **(self.ground_counts if self.config.ground_contact_rule != "none" else {})},
                "tracks": {k: v.to_doc(self.fps) for k, v in sorted(self.tracks.items(), key=lambda kv: int(kv[0].split("-")[1]))},
                "track_losses": sum(len(v.losses) for v in self.tracks.values()),
                "detector_ms_per_frame": round(1000 * self.t_detect / n, 2), "tracker_ms_per_frame": round(1000 * self.t_track / n, 2)}


def track_video(video_path: Path, config: VideoTrackingConfig, *, detector=None, model_info: dict | None = None, max_frames: int | None = None,
                on_frame=None, progress=None) -> tuple[ObservationFile, dict]:
    """OFFLINE FILE ANALYSIS: detect and track people in every decoded frame, as fast as the machine allows. Observation
    times are the original video-relative times (frame / fps) whatever the processing speed; the log reports source
    duration, processing duration, throughput and whether processing kept up with the video's own rate. This is not
    live processing: see camera_adapter/bounded.py for the bounded path. `detector` is any callable frame -> (boxes,
    scores); `on_frame(frame_index, frame, tracked)` receives [(track_id, box, score, used)] for previews."""
    import cv2  # noqa: PLC0415

    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"cannot open video {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    declared = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not (math.isfinite(fps) and fps > 0) or width <= 0 or height <= 0:
        cap.release()
        raise ValueError(f"{video_path}: the decoder reports no usable frame rate or size (fps {fps}, {width}x{height})")
    ft = FrameTracker(config, detector, fps, width, height)
    mismatched: list[dict] = []
    frames = 0
    started = time.perf_counter()
    period_ms = 1000.0 / fps
    while max_frames is None or frames < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        f = frames
        pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
        if math.isfinite(pos_ms) and pos_ms >= 0 and f > 0 and abs(pos_ms - f * period_ms) > config.max_time_mismatch_frames * period_ms:
            if len(mismatched) < 200:
                mismatched.append({"frame": f, "expected_ms": round(f * period_ms, 3), "decoder_ms": round(pos_ms, 3)})
            else:
                mismatched.append(None)
        shown = ft.process(f, f / fps, frame)
        if on_frame is not None:
            on_frame(f, frame, shown)
        frames += 1
        if progress is not None and frames % 100 == 0:
            progress(frames, declared)
    cap.release()
    elapsed = time.perf_counter() - started
    if frames == 0:
        raise ValueError(f"{video_path}: no frame could be decoded")
    obs = sorted(ft.obs, key=lambda o: (o.frame, o.track_id))
    track = ft.summary()
    source_s = frames / fps
    log = {
        "format": "athletrix-video-tracking-log", "format_version": 2, "mode": "offline_file",
        "video": {"file": Path(video_path).name, "sha256": sha256_file(video_path), "width": width, "height": height, "fps": fps,
                  "declared_frame_count": declared, "decoded_frames": frames, "time_base": "video_relative (frame / fps); not camera capture time",
                  "stopped_by_max_frames": max_frames is not None and frames >= max_frames,
                  "undecoded_frames_vs_declared": max(0, declared - frames) if declared > 0 and not (max_frames is not None and frames >= max_frames) else None,
                  "decoder_time_mismatches": len(mismatched), "decoder_time_mismatch_examples": [m for m in mismatched if m][:20],
                  "note": "a mismatch is a decoded frame whose decoder timestamp differs from frame / fps by more than max_time_mismatch_frames periods: a dropped or duplicated frame or a variable frame rate. Observations still use frame / fps; re-encode to a constant frame rate if mismatches are reported."},
        "model": model_info, "config": config.to_doc(), "versions": package_versions(),
        **{k: track[k] for k in ("detections", "observations", "skipped", "ground_contact", "tracks", "track_losses")},
        "speed": {"mode": "offline file analysis: every frame, as fast as possible; observation times stay video-relative",
                  "source_duration_s": round(source_s, 3), "processing_duration_s": round(elapsed, 3), "wall_seconds": round(elapsed, 3),
                  "frames_per_second": round(frames / elapsed, 3) if elapsed > 0 else None, "video_seconds": round(source_s, 3),
                  "detector_ms_per_frame": track["detector_ms_per_frame"], "tracker_ms_per_frame": track["tracker_ms_per_frame"],
                  "realtime_factor": round(source_s / elapsed, 3) if elapsed > 0 else None, "kept_up_with_source_rate": elapsed <= source_s,
                  "note": "kept_up_with_source_rate compares this run's processing time with the video's duration; it says nothing about a live camera, whose frames cannot wait."},
        "identity": "track ids are temporary and unassigned (athlete_id null); assign tracks explicitly after review. No jersey recognition, re-identification or role classification is performed.",
        "python": sys.version.split()[0],
    }
    extractor = f"person detection {model_info['name'] if model_info else 'fixture'} + {config.tracker.implementation} (video tracking v1)"
    return ObservationFile(Video(width, height, round(fps, 6), Path(video_path).name), extractor, obs), log


def ground_status(reference: list[float], box_height: float, config: VideoTrackingConfig) -> str | None:
    """Ground-contact evidence for one box of a track (see GROUND above); updates the track's reference in place."""
    if config.ground_contact_rule == "none":
        return None
    if len(reference) < config.ground_min_reference:
        status = "unknown"
    else:
        ref = sorted(reference)[len(reference) // 2]
        status = "suspect" if box_height < config.ground_min_height_ratio * ref else "consistent"
    if status != "suspect":
        reference.append(box_height)
        del reference[:-config.ground_reference_frames]
    return status


def assignment_template(log: dict) -> dict:
    """An assignments document with no assignments and every track listed for review: an operator adds the confirmed ones."""
    return {"format": "athletrix-track-assignments", "format_version": 1, "assignments": [],
            "tracks_to_review": [{"track_id": k, **{x: v[x] for x in ("first_frame", "last_frame", "first_video_time_s", "last_video_time_s", "observations")},
                                  "losses": len(v["losses"])} for k, v in log["tracks"].items()],
            "instructions": "Review the track gallery or annotated video. For each track you have confirmed, add {\"track_id\", \"athlete_id\", \"from_frame\", \"to_frame\", \"assigned_by\"} to assignments. Leave new, uncertain or non-player tracks out: unassigned tracks never produce athlete samples. A track id can move between people after a crossing; limit an assignment's frame range to what you verified."}


class PreviewWriter:
    """Assignment aid: an annotated video (boxes and temporary track ids) and a gallery image with one crop per track."""

    BANNER = "ASSIGNMENT AID - temporary track ids, not verified identity"

    def __init__(self, video_out: Path | None, gallery_out: Path | None, fps: float, size: tuple[int, int]) -> None:
        import cv2  # noqa: PLC0415
        self._cv2 = cv2
        self.writer = None
        if video_out is not None:
            self.writer = cv2.VideoWriter(str(video_out), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
            if not self.writer.isOpened():
                raise RuntimeError(f"OpenCV could not open a video writer for {video_out}")
        self.gallery_out = gallery_out
        self.crops: dict[str, list] = {}    # track_id -> [(frame, crop)] (first, then the largest box later)
        self.fps = fps

    def __call__(self, f, frame, shown) -> None:
        cv2 = self._cv2
        if self.gallery_out is not None:
            for tid, (x1, y1, x2, y2), score, used in shown:
                if tid is None:
                    continue
                area = (x2 - x1) * (y2 - y1)
                have = self.crops.setdefault(tid, [])
                if len(have) < 1 or (len(have) == 1 and area > 1.3 * have[0][2]) or (len(have) == 2 and area > have[1][2]):
                    crop = frame[max(0, int(y1)):int(y2) + 1, max(0, int(x1)):int(x2) + 1].copy()
                    if crop.size:
                        entry = (f, crop, area)
                        if len(have) < 2:
                            have.append(entry)
                        else:
                            have[1] = entry
        if self.writer is not None:
            img = frame.copy()
            for tid, (x1, y1, x2, y2), score, used in shown:
                colour = (0, 200, 255) if tid is None else ((60, 220, 60) if used else (160, 160, 160))
                cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), colour, 2)
                label = f"{tid or 'unconfirmed'} {score:.2f}" + ("" if used or tid is None else " (not used)")
                cv2.putText(img, label, (int(x1), max(12, int(y1) - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)
                cv2.circle(img, (int((x1 + x2) / 2), int(y2)), 3, colour, -1)
            cv2.rectangle(img, (0, 0), (img.shape[1], 22), (0, 0, 0), -1)
            cv2.putText(img, f"{self.BANNER} | frame {f} t={f / self.fps:.2f}s (video time)", (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            self.writer.write(img)

    def close(self) -> None:
        if self.writer is not None:
            self.writer.release()
        if self.gallery_out is not None:
            self._write_gallery()

    def _write_gallery(self) -> None:
        cv2 = self._cv2
        import numpy as np  # noqa: PLC0415
        tile_w, tile_h, cols = 150, 190, 8
        items = sorted(self.crops.items(), key=lambda kv: int(kv[0].split("-")[1]))
        rows = max(1, math.ceil(len(items) / cols))
        sheet = np.full((40 + rows * tile_h, cols * tile_w, 3), 30, np.uint8)
        cv2.putText(sheet, self.BANNER, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        for i, (tid, crops) in enumerate(items):
            r, c = divmod(i, cols)
            x0, y0 = c * tile_w, 40 + r * tile_h
            for j, (f, crop, _) in enumerate(crops[:2]):
                h, w = crop.shape[:2]
                s = min((tile_w / 2 - 6) / w, (tile_h - 40) / h)
                small = cv2.resize(crop, (max(1, int(w * s)), max(1, int(h * s))))
                sx, sy = x0 + 3 + j * (tile_w // 2), y0 + 4
                sheet[sy:sy + small.shape[0], sx:sx + small.shape[1]] = small
            f0 = crops[0][0]
            cv2.putText(sheet, tid, (x0 + 4, y0 + tile_h - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 220, 60), 1, cv2.LINE_AA)
            cv2.putText(sheet, f"from frame {f0} ({f0 / self.fps:.1f}s)", (x0 + 4, y0 + tile_h - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (220, 220, 220), 1, cv2.LINE_AA)
        cv2.imwrite(str(self.gallery_out), sheet)


def load_config(path: Path | None) -> VideoTrackingConfig:
    if path is None:
        return VideoTrackingConfig()
    return VideoTrackingConfig.from_doc(json.loads(Path(path).read_text(encoding="utf-8")))
