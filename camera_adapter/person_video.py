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
# the ones observed when the official release assets were first fetched for this project (2026-10-29); the upstream
# release publishes no checksums of its own, so this pin detects a changed file, it does not prove the original.
MODELS = {
    "yolox_s": {"url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx", "bytes": 35858002,
                "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063", "input_size": 640,
                "licence": "Apache-2.0 (Megvii YOLOX repository and release); trained on COCO 2017",
                "source": "Megvii-BaseDetection/YOLOX release 0.1.1rc0, official ONNX export"},
    "yolox_tiny": {"url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx", "bytes": 20219662,
                   "sha256": None, "input_size": 416,
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


def track_video(video_path: Path, config: VideoTrackingConfig, *, detector=None, model_info: dict | None = None, max_frames: int | None = None,
                on_frame=None, progress=None) -> tuple[ObservationFile, dict]:
    """Detect and track people in every decoded frame. `detector` is any callable frame -> (boxes, scores) (tests pass a
    fixture detector). `on_frame(frame_index, frame, tracked)` receives [(track_id, box, score, used)] for previews."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import supervision as sv  # noqa: PLC0415
    from trackers import ByteTrackTracker  # noqa: PLC0415

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
    t = config.tracker
    tracker = ByteTrackTracker(lost_track_buffer=t.lost_track_buffer, frame_rate=fps, track_activation_threshold=t.track_activation_threshold,
                               minimum_consecutive_frames=t.minimum_consecutive_frames, minimum_iou_threshold=t.minimum_iou_threshold,
                               high_conf_det_threshold=t.high_conf_det_threshold)
    obs: list[Observation] = []
    tracks: dict[str, TrackStats] = {}
    skipped = {"unconfirmed_track": 0, "box_touches_bottom_edge": 0, "box_too_small": 0}
    mismatched: list[dict] = []
    detections_total, frames = 0, 0
    t_detect = t_track = 0.0
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
        a = time.perf_counter()
        boxes, scores = detector(frame)
        b = time.perf_counter()
        boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
        scores = np.asarray(scores, dtype=np.float32).reshape(-1)
        detections_total += len(scores)
        det = sv.Detections(xyxy=boxes, confidence=scores, class_id=np.zeros(len(scores), dtype=int))
        tracked = tracker.update(det, timestamp=f / fps)
        c = time.perf_counter()
        t_detect += b - a
        t_track += c - b
        shown = []
        for box, score, tid in zip(tracked.xyxy, tracked.confidence if tracked.confidence is not None else np.ones(len(tracked)), tracked.tracker_id):
            x1, y1, x2, y2 = (float(v) for v in box)
            if tid is None or int(tid) < 0:
                skipped["unconfirmed_track"] += 1
                shown.append((None, (x1, y1, x2, y2), float(score), False))
                continue
            track_id = f"trk-{int(tid)}"
            used = True
            if config.drop_boxes_touching_bottom_edge and y2 >= height - 1.5:
                skipped["box_touches_bottom_edge"] += 1
                used = False
            elif y2 - y1 < config.min_box_height_px:
                skipped["box_too_small"] += 1
                used = False
            st = tracks.get(track_id)
            if st is None:
                st = tracks[track_id] = TrackStats(f, f)
            elif f > st.last_frame + 1:
                st.losses.append([st.last_frame, f])
            st.last_frame = f
            shown.append((track_id, (x1, y1, x2, y2), float(score), used))
            if used:
                st.observations += 1
                u = min(max((x1 + x2) / 2, -0.5), width - 0.5)
                v = min(max(y2, -0.5), height - 0.5)
                obs.append(Observation(f, round(f / fps, 6), track_id, round(u, 3), round(v, 3), round(min(1.0, max(0.0, float(score))), 4), None))
        if on_frame is not None:
            on_frame(f, frame, shown)
        frames += 1
        if progress is not None and frames % 100 == 0:
            progress(frames, declared)
    cap.release()
    elapsed = time.perf_counter() - started
    if frames == 0:
        raise ValueError(f"{video_path}: no frame could be decoded")
    obs.sort(key=lambda o: (o.frame, o.track_id))
    n_mismatch = len(mismatched)
    log = {
        "format": "athletrix-video-tracking-log", "format_version": 1,
        "video": {"file": Path(video_path).name, "sha256": sha256_file(video_path), "width": width, "height": height, "fps": fps,
                  "declared_frame_count": declared, "decoded_frames": frames, "time_base": "video_relative (frame / fps); not camera capture time",
                  "stopped_by_max_frames": max_frames is not None and frames >= max_frames,
                  "undecoded_frames_vs_declared": max(0, declared - frames) if declared > 0 and not (max_frames is not None and frames >= max_frames) else None,
                  "decoder_time_mismatches": n_mismatch, "decoder_time_mismatch_examples": [m for m in mismatched if m][:20],
                  "note": "a mismatch is a decoded frame whose decoder timestamp differs from frame / fps by more than max_time_mismatch_frames periods: a dropped or duplicated frame or a variable frame rate. Observations still use frame / fps; re-encode to a constant frame rate if mismatches are reported."},
        "model": model_info, "config": config.to_doc(), "versions": package_versions(),
        "detections": detections_total, "observations": len(obs), "skipped": skipped,
        "tracks": {k: v.to_doc(fps) for k, v in sorted(tracks.items(), key=lambda kv: int(kv[0].split("-")[1]))},
        "track_losses": sum(len(v.losses) for v in tracks.values()),
        "speed": {"wall_seconds": round(elapsed, 3), "frames_per_second": round(frames / elapsed, 3) if elapsed > 0 else None,
                  "detector_ms_per_frame": round(1000 * t_detect / frames, 2), "tracker_ms_per_frame": round(1000 * t_track / frames, 2),
                  "video_seconds": round(frames / fps, 3), "realtime_factor": round((frames / fps) / elapsed, 3) if elapsed > 0 else None},
        "identity": "track ids are temporary and unassigned (athlete_id null); assign tracks explicitly after review. No jersey recognition, re-identification or role classification is performed.",
        "python": sys.version.split()[0],
    }
    extractor = f"person detection {model_info['name'] if model_info else 'fixture'} + {config.tracker.implementation} (video tracking v1)"
    return ObservationFile(Video(width, height, round(fps, 6), Path(video_path).name), extractor, obs), log


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
