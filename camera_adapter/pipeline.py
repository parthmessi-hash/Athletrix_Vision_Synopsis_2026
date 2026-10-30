"""Observations + calibration + adapter configuration -> TrackingSample records, with a complete adapter log.

Per observation, in video-time order:
  1. no explicit athlete assignment             -> skipped ("unassigned"); never sent under any athlete id
  2. two tracks assigned to one athlete in a frame -> both skipped ("assignment_conflict") and the athlete's history reset
  3. confidence below min_observation_confidence -> not used ("low_confidence")
  3b. ground_contact "suspect" with ground_contact_policy "exclude_suspect" (default) -> not used ("ground_contact_suspect"),
     and the athlete's next sample starts a new continuity segment: a confident detection whose ground point is in
     doubt is not a usable position
  4. maps beyond the horizon, or more than max_extrapolation_m outside the calibrated points' field extent
                                                 -> not used ("outside_calibration")
  5. otherwise the field position enters the athlete's causal estimator (camera_adapter/estimator.py), which may
     reset on gaps or track changes and skip while warming up; an estimate is emitted no faster than
     output_max_rate_hz, at the time of a real observation.
A calibration that fails validation stops everything: nothing athlete-specific is produced from an unusable mapping.

CONFIDENCE: tracking_confidence = the lowest observation confidence among the observations in the estimate's window
(rule "min_observation_confidence_in_window"). It is the extractor's score carried through, not a probability that the
position is right. The engine excludes a distance interval when either end is below its own
DISTANCE_MIN_CONFIDENCE (0.5 by default); this adapter neither knows nor adjusts that threshold.

CONTINUITY: every sample carries a segment_id (unless the configuration turns labels off). A new segment starts for an
athlete at its first sample and after every estimator reset (gap longer than max_gap_s, track change, assignment
conflict) or suppressed estimate (fit residual too large) since its previous sample; rate-limit skips do not break a
segment. The engine never integrates distance between samples of different segments, so a known camera interruption
cannot be bridged by the engine's own, longer DISTANCE_MAX_GAP_SECONDS. Labels are "cam-<run tag>-<n>": the run tag
is derived from the inputs and the time mapping, so the same file sent twice at different anchors gets different
labels. A label joins samples; it is not an identity or a quality score.

VALUE BASIS: every sample is sent with value_basis "estimated" (a causal fit of real observations); nothing is
predicted or extrapolated, and skipped instants are unavailable (no sample). With the estimator's optional stationary
rule, a sample whose motion is not distinguishable from fit noise is reported at speed 0 and counted in the log
(per sample `stationary_within_noise`), never silently.

TIME: sample timestamp = anchor_utc + (video_time_s - video_origin_s). The anchor is stated explicitly by whoever runs
the adapter and recorded in the log with its meaning; for files without a known capture-time mapping it is a playback
or analysis anchor, NOT the time the camera captured the frames.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from camera_adapter.calibration import Calibration
from camera_adapter.estimator import AthleteEstimator, EstimatorConfig, Point
from camera_adapter.observations import ObservationFile

CONFIG_FORMAT = "athletrix-camera-adapter-config"
SOURCES = ("camera", "simulated")   # existing SampleSource values an adapter may state; synthetic input must say "simulated"


@dataclass(frozen=True)
class AdapterConfig:
    source: str = "simulated"
    device_id: str = "synthetic-marker-cam-1"
    output_max_rate_hz: float = 10.0
    min_observation_confidence: float = 0.25
    max_extrapolation_m: float = 2.0
    confidence_rule: str = "min_observation_confidence_in_window"
    emit_segment_id: bool = True
    ground_contact_policy: str = "exclude_suspect"     # or "use_all"
    estimator: EstimatorConfig = field(default_factory=EstimatorConfig)

    def validate(self) -> list[str]:
        e = self.estimator.validate()
        if self.source not in SOURCES:
            e.append(f"source must be one of {SOURCES}")
        if not self.device_id.strip():
            e.append("device_id must be non-blank")
        if not (0 < self.output_max_rate_hz <= 10):
            e.append("output_max_rate_hz must be in (0, 10]")
        if not (0 <= self.min_observation_confidence <= 1):
            e.append("min_observation_confidence must be in [0, 1]")
        if not (self.max_extrapolation_m >= 0):
            e.append("max_extrapolation_m must be >= 0")
        if self.ground_contact_policy not in ("exclude_suspect", "use_all"):
            e.append("ground_contact_policy must be exclude_suspect or use_all")
        if not isinstance(self.emit_segment_id, bool):
            e.append("emit_segment_id must be true or false")
        if self.confidence_rule != "min_observation_confidence_in_window":
            e.append("confidence_rule must be min_observation_confidence_in_window (the only rule in version 1)")
        return e

    def to_doc(self) -> dict:
        return {"format": CONFIG_FORMAT, "format_version": 1, **asdict(self)}

    @classmethod
    def from_doc(cls, doc: dict) -> "AdapterConfig":
        if doc.get("format") != CONFIG_FORMAT or doc.get("format_version") != 1:
            raise ValueError(f"not an {CONFIG_FORMAT} version 1 document")
        body = {k: v for k, v in doc.items() if k not in ("format", "format_version")}
        est = EstimatorConfig(**body.pop("estimator", {}))
        cfg = cls(**body, estimator=est)
        errors = cfg.validate()
        if errors:
            raise ValueError("; ".join(errors))
        return cfg

    def as_recording_metadata(self, calibration_sha256: str, mapping: "TimeMapping | None") -> dict:
        """Flat key/values for POST /recordings adapter_configuration.metadata (at most 32 keys; supplied, not verified)."""
        md = {"adapter": "camera_adapter v1 (synthetic marker tracking input)", "source": self.source, "device_id": self.device_id,
              "output_max_rate_hz": self.output_max_rate_hz, "min_observation_confidence": self.min_observation_confidence,
              "max_extrapolation_m": self.max_extrapolation_m, "confidence_rule": self.confidence_rule, "emit_segment_id": self.emit_segment_id, "ground_contact_policy": self.ground_contact_policy,
              "calibration_sha256": calibration_sha256[:64]}
        md.update({f"estimator.{k}": v for k, v in asdict(self.estimator).items()})
        if mapping is not None:
            md["time_mapping"] = mapping.describe()[:200]
        return md


@dataclass(frozen=True)
class TimeMapping:
    anchor_utc: datetime
    video_origin_s: float = 0.0
    meaning: str = "playback anchor stated by the operator; not camera capture time"

    def to_utc(self, video_time_s: float) -> datetime:
        return self.anchor_utc + timedelta(seconds=video_time_s - self.video_origin_s)

    def describe(self) -> str:
        return f"timestamp = {self.anchor_utc.isoformat().replace('+00:00', 'Z')} + (video_time_s - {self.video_origin_s:g}); {self.meaning}"


@dataclass
class Output:
    athlete_id: str
    video_time_s: float
    frame: int
    track_id: str
    x: float
    y: float
    speed_mps: float
    acceleration_mps2: float
    confidence: float
    points: int
    rms_m: float
    segment: int = 0
    stationary_within_noise: bool = False
    speed_sigma_mps: float = 0.0

    def sample(self, mapping: TimeMapping, config: AdapterConfig, run_tag: str = "") -> dict:
        """A TrackingSample payload (validated by the engine's model before it is written or sent)."""
        extra = {"segment_id": f"cam-{run_tag}-{self.segment}"} if config.emit_segment_id else {}
        return {"athlete_id": self.athlete_id, "timestamp": mapping.to_utc(self.video_time_s).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "x_position": round(self.x, 4), "y_position": round(self.y, 4), "speed_mps": round(self.speed_mps, 4),
                "acceleration_mps2": round(self.acceleration_mps2, 4), "tracking_confidence": round(min(1.0, max(0.0, self.confidence)), 4),
                "source": config.source, "device_id": config.device_id, "value_basis": "estimated", **extra}


@dataclass
class Result:
    outputs: list[Output] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)          # reason -> count
    resets: list[dict] = field(default_factory=list)     # {"athlete_id", "video_time_s", "reason"}
    unassigned: dict = field(default_factory=dict)       # track_id -> [first_frame, last_frame, count]
    per_athlete: dict = field(default_factory=dict)      # athlete_id -> {"observations", "outputs", "warm_up"}
    segments: list[dict] = field(default_factory=list)   # {"segment", "athlete_id", "started_by", "first_video_time_s", "last_video_time_s", "samples"}

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def process(obs: ObservationFile, calibration: Calibration, config: AdapterConfig) -> Result:
    errors = config.validate()
    if errors:
        raise ValueError("; ".join(errors))
    if (obs.video.width, obs.video.height) != (calibration.width, calibration.height):
        raise ValueError(f"the observations come from a {obs.video.width}x{obs.video.height} video but the calibration is for {calibration.width}x{calibration.height}; nothing was produced")
    res = Result()
    estimators: dict[str, AthleteEstimator] = {}
    last_out: dict[str, float] = {}
    x0, y0, x1, y1 = calibration.field_bounds()
    m = config.max_extrapolation_m
    by_frame: dict[int, list] = {}
    for o in obs.observations:
        by_frame.setdefault(o.frame, []).append(o)
    period = 1.0 / config.output_max_rate_hz
    broken: dict[str, str] = {}        # athlete_id -> why the next sample starts a new segment
    open_segment: dict[str, dict] = {}
    for frame in sorted(by_frame):
        group = sorted(by_frame[frame], key=lambda o: o.track_id)
        counts: dict[str, int] = {}
        for o in group:
            if o.athlete_id:
                counts[o.athlete_id] = counts.get(o.athlete_id, 0) + 1
        for o in group:
            if not o.athlete_id:
                u = res.unassigned.setdefault(o.track_id, [o.frame, o.frame, 0])
                u[1] = o.frame; u[2] += 1
                res.skip("unassigned")
                continue
            stats = res.per_athlete.setdefault(o.athlete_id, {"observations": 0, "outputs": 0, "warm_up": 0, "fit_residual": 0})
            stats["observations"] += 1
            est = estimators.setdefault(o.athlete_id, AthleteEstimator(config.estimator))
            if counts[o.athlete_id] > 1:
                res.skip("assignment_conflict")
                if est.history:
                    est.history.clear()
                    broken.setdefault(o.athlete_id, "assignment_conflict")
                    res.resets.append({"athlete_id": o.athlete_id, "video_time_s": o.video_time_s, "reason": "assignment_conflict"})
                continue
            if o.confidence < config.min_observation_confidence:
                res.skip("low_confidence")
                continue
            if o.ground_contact == "suspect" and config.ground_contact_policy == "exclude_suspect":
                res.skip("ground_contact_suspect")
                stats["ground_contact_suspect"] = stats.get("ground_contact_suspect", 0) + 1
                broken.setdefault(o.athlete_id, "ground_contact_suspect")
                continue
            pos = calibration.to_field(o.u_px, o.v_px)
            if pos is None or not (x0 - m <= pos[0] <= x1 + m and y0 - m <= pos[1] <= y1 + m):
                res.skip("outside_calibration")
                continue
            reason = est.add(Point(o.video_time_s, pos[0], pos[1], o.confidence, o.track_id))
            if reason:
                broken.setdefault(o.athlete_id, reason)
                res.resets.append({"athlete_id": o.athlete_id, "video_time_s": o.video_time_s, "reason": reason})
            if o.athlete_id in last_out and o.video_time_s - last_out[o.athlete_id] < period - 1e-6:
                res.skip("rate_limit")
                continue
            e, why = est.estimate()
            if e is None:
                res.skip(why)
                stats[why] = stats.get(why, 0) + 1
                if why != "warm_up":   # a suppressed estimate inside a history: the samples either side are not one segment
                    broken.setdefault(o.athlete_id, why)
                continue
            last_out[o.athlete_id] = o.video_time_s
            stats["outputs"] += 1
            seg = open_segment.get(o.athlete_id)
            if seg is None or o.athlete_id in broken:
                seg = {"segment": len(res.segments) + 1, "athlete_id": o.athlete_id, "started_by": broken.pop(o.athlete_id, "first_sample") if seg else "first_sample",
                       "first_video_time_s": o.video_time_s, "last_video_time_s": o.video_time_s, "samples": 0}
                broken.pop(o.athlete_id, None)
                res.segments.append(seg)
                open_segment[o.athlete_id] = seg
            seg["last_video_time_s"] = o.video_time_s
            seg["samples"] += 1
            if e.stationary_within_noise:
                stats["stationary_within_noise"] = stats.get("stationary_within_noise", 0) + 1
            res.outputs.append(Output(o.athlete_id, o.video_time_s, o.frame, o.track_id, e.x, e.y, e.speed_mps, e.acceleration_mps2, e.confidence, e.points, e.rms_m, seg["segment"],
                                      e.stationary_within_noise, e.speed_sigma_mps))
    return res


def run_tag(observations_sha256: str, mapping: TimeMapping) -> str:
    """Short label part derived from the input file and the time mapping: a rerun of the same file at another anchor differs."""
    return hashlib.sha256(f"{observations_sha256}|{mapping.describe()}".encode()).hexdigest()[:10]


def adapter_log(res: Result, *, obs: ObservationFile, calibration_path: Path, calibration: Calibration, config: AdapterConfig, mapping: TimeMapping | None, observations_path: Path) -> dict:
    return {
        "format": "athletrix-camera-adapter-log", "format_version": 1,
        "input": {"observations": str(Path(observations_path).name), "observations_sha256": file_sha256(observations_path), "extractor": obs.extractor,
                  "video": asdict(obs.video), "time_base": "video_relative", "observation_count": len(obs.observations)},
        "calibration": {"file": str(Path(calibration_path).name), "sha256": file_sha256(calibration_path), "camera_id": calibration.camera_id,
                        "fit_residual_rms_m": calibration.fit_residual_m.get("rms"), "check_errors_m": None if not calibration.check_errors_m else {k: calibration.check_errors_m[k] for k in ("rms", "max")}},
        "config": config.to_doc(),
        "time_mapping": None if mapping is None else {"anchor_utc": mapping.anchor_utc.isoformat().replace("+00:00", "Z"), "video_origin_s": mapping.video_origin_s, "meaning": mapping.meaning, "rule": mapping.describe()},
        "outputs": len(res.outputs), "per_athlete": res.per_athlete, "skipped": res.skipped, "resets": res.resets,
        "segments": [{**sg, "segment_id": f"cam-{run_tag(file_sha256(observations_path), mapping)}-{sg['segment']}" if mapping and config.emit_segment_id else None} for sg in res.segments],
        "unassigned_tracks": {k: {"first_frame": v[0], "last_frame": v[1], "observations": v[2]} for k, v in res.unassigned.items()},
        "notes": ["Skipped samples were never produced: no value is filled in for a skipped instant.",
                  "Each output keeps its video_time_s and frame in the per-sample log so the original relative time is preserved.",
                  "tracking_confidence is the extractor's score carried through (lowest in the window), not a probability of correctness.",
                  "A new segment_id starts after every estimator reset or suppressed estimate; the engine never integrates distance between different segments."],
        "samples": [{"athlete_id": o.athlete_id, "frame": o.frame, "video_time_s": o.video_time_s, "track_id": o.track_id, "points": o.points, "fit_rms_m": round(o.rms_m, 5),
                     "speed_sigma_mps": round(o.speed_sigma_mps, 5), **({"stationary_within_noise": True} if o.stationary_within_noise else {})} for o in res.outputs],
    }


def records(res: Result, mapping: TimeMapping, config: AdapterConfig, tag: str = "") -> list[dict]:
    """Interchange records ({"type": "tracking", "sample": ...}), validated against the engine's TrackingSample model.

    `tag` is the run tag for segment labels (run_tag(); callers with an input file pass it so reruns stay distinct)."""
    from app.models import TrackingSample
    tag = tag or hashlib.sha256(mapping.describe().encode()).hexdigest()[:10]
    out = []
    for o in res.outputs:
        s = o.sample(mapping, config, tag)
        TrackingSample.model_validate(s)
        if not all(math.isfinite(s[k]) for k in ("x_position", "y_position", "speed_mps", "acceleration_mps2")):
            raise ValueError("non-finite estimate")
        out.append({"type": "tracking", "sample": s})
    return out


def write_jsonl(recs: list[dict], path: Path) -> None:
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
