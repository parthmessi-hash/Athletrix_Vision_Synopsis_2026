"""Camera observation contract (internal adapter format, version 1): what a frame-level extractor hands to the adapter.

FILE: JSON Lines, UTF-8. Line 1 is a header, every further line one observation.

  header       {"format": "athletrix-camera-observations", "format_version": 1,
                "video": {"width": 960, "height": 540, "fps": 20.0, "name": "synthetic.avi"},
                "time_base": "video_relative", "extractor": "synthetic marker tracking v1"}
  observation  {"frame": 12, "video_time_s": 0.6, "track_id": "trk-1", "u_px": 412.3, "v_px": 288.1,
                "confidence": 0.97, "athlete_id": null}

Meaning of each field
  frame         0-based index of the decoded frame the observation comes from.
  video_time_s  seconds since the first frame of the video (VIDEO-RELATIVE time, sub-second precision). For a file
                with a constant frame rate it is frame / fps. It is NOT camera capture time, file modification time or
                processing time; mapping it to UTC is a separate, explicit step (pipeline.TimeMapping).
  track_id      temporary id chosen by the extractor for one continuous track. It says "the same blob as in the
                previous frames", nothing about who it is. It must never equal an athlete id; a target that disappears
                and reappears may get a new track id.
  u_px, v_px    image position of the target's ground-contact point, in pixels of the full frame: OpenCV convention,
                (0, 0) is the centre of the top-left pixel, u to the right, v downwards; sub-pixel values allowed.
  confidence    the extractor's own score in [0, 1] (for synthetic markers: visible fraction of the marker's area).
                It is NOT a calibrated probability that the position or identity is correct.
  athlete_id    the athlete this track is explicitly assigned to, or null when unknown. Null observations are never
                sent under any athlete id; assignment comes from the extractor's operator or an assignment file.

Validation is whole-file and strict (see `load`): unknown keys, non-finite numbers, positions outside the image,
frames out of order within a track, two observations of one track in one frame, inconsistent times and track ids that
collide with athlete ids are errors, reported per line.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

FORMAT = "athletrix-camera-observations"
FORMAT_VERSION = 1
HEADER_KEYS = {"format", "format_version", "video", "time_base", "extractor"}
VIDEO_KEYS = {"width", "height", "fps", "name"}
OBS_KEYS = {"frame", "video_time_s", "track_id", "u_px", "v_px", "confidence", "athlete_id"}
TIME_TOLERANCE_S = 1e-3   # video_time_s must agree with frame / fps within this


@dataclass(frozen=True)
class Video:
    width: int
    height: int
    fps: float
    name: str = ""


@dataclass(frozen=True)
class Observation:
    frame: int
    video_time_s: float
    track_id: str
    u_px: float
    v_px: float
    confidence: float
    athlete_id: str | None = None


@dataclass
class ObservationFile:
    video: Video
    extractor: str
    observations: list[Observation] = field(default_factory=list)


class ObservationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]) + (f" (and {len(errors) - 5} more)" if len(errors) > 5 else ""))
        self.errors = errors


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse(lines: list[str]) -> ObservationFile:
    errors: list[str] = []
    if not lines:
        raise ObservationError(["empty file: a header line is required"])
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ObservationError([f"line 1: header is not valid JSON ({exc.msg})"]) from exc
    if not isinstance(header, dict) or set(header) != HEADER_KEYS or header.get("format") != FORMAT or header.get("format_version") != FORMAT_VERSION:
        raise ObservationError([f"line 1: header must have exactly {sorted(HEADER_KEYS)} with format {FORMAT!r} version {FORMAT_VERSION}"])
    if header["time_base"] != "video_relative":
        errors.append("line 1: time_base must be 'video_relative' (this version has no capture-time mapping)")
    v = header["video"]
    if not isinstance(v, dict) or set(v) != VIDEO_KEYS or not all(isinstance(v.get(k), int) and not isinstance(v.get(k), bool) and v[k] > 0 for k in ("width", "height")) \
            or not _finite(v.get("fps")) or v["fps"] <= 0 or not isinstance(v.get("name"), str):
        raise ObservationError(errors + ["line 1: video must be {width, height (positive integers), fps (positive), name}"])
    video = Video(v["width"], v["height"], float(v["fps"]), v["name"])
    obs: list[Observation] = []
    last_frame: dict[str, int] = {}
    seen: set[tuple[int, str]] = set()
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {n}: not valid JSON ({exc.msg})")
            continue
        if not isinstance(r, dict) or set(r) != OBS_KEYS:
            errors.append(f"line {n}: an observation has exactly the keys {sorted(OBS_KEYS)}")
            continue
        problems = []
        if not isinstance(r["frame"], int) or isinstance(r["frame"], bool) or r["frame"] < 0:
            problems.append("frame must be a non-negative integer")
        if not _finite(r["video_time_s"]) or r["video_time_s"] < 0:
            problems.append("video_time_s must be a finite number >= 0")
        elif isinstance(r["frame"], int) and abs(r["video_time_s"] - r["frame"] / video.fps) > TIME_TOLERANCE_S:
            problems.append(f"video_time_s {r['video_time_s']} does not equal frame / fps ({r['frame'] / video.fps:.6f})")
        if not isinstance(r["track_id"], str) or not r["track_id"].strip():
            problems.append("track_id must be a non-blank string")
        if not _finite(r["u_px"]) or not _finite(r["v_px"]):
            problems.append("u_px and v_px must be finite numbers")
        elif not (-0.5 <= r["u_px"] <= video.width - 0.5 and -0.5 <= r["v_px"] <= video.height - 0.5):
            problems.append(f"position ({r['u_px']}, {r['v_px']}) lies outside the {video.width}x{video.height} image")
        if not _finite(r["confidence"]) or not 0 <= r["confidence"] <= 1:
            problems.append("confidence must be a number in [0, 1]")
        if r["athlete_id"] is not None and (not isinstance(r["athlete_id"], str) or not r["athlete_id"].strip()):
            problems.append("athlete_id must be null or a non-blank string")
        if problems:
            errors.append(f"line {n}: " + "; ".join(problems))
            continue
        key = (r["frame"], r["track_id"])
        if key in seen:
            errors.append(f"line {n}: track {r['track_id']!r} has two observations in frame {r['frame']}")
            continue
        if r["track_id"] in last_frame and r["frame"] <= last_frame[r["track_id"]]:
            errors.append(f"line {n}: frames of track {r['track_id']!r} must increase")
            continue
        seen.add(key)
        last_frame[r["track_id"]] = r["frame"]
        obs.append(Observation(r["frame"], float(r["video_time_s"]), r["track_id"], float(r["u_px"]), float(r["v_px"]), float(r["confidence"]), r["athlete_id"]))
    athletes = {o.athlete_id for o in obs if o.athlete_id}
    clash = sorted({o.track_id for o in obs} & athletes)
    if clash:
        errors.append(f"track ids must differ from athlete ids: {', '.join(clash)}")
    if errors:
        raise ObservationError(errors)
    return ObservationFile(video=video, extractor=header["extractor"], observations=obs)


def load(path: Path) -> ObservationFile:
    return parse(Path(path).read_text(encoding="utf-8").splitlines())


def dump(f: ObservationFile, path: Path) -> None:
    header = {"format": FORMAT, "format_version": FORMAT_VERSION, "video": asdict(f.video), "time_base": "video_relative", "extractor": f.extractor}
    lines = [json.dumps(header)] + [json.dumps(asdict(o)) for o in f.observations]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- explicit assignment of temporary tracks to athletes -----------------------------------------------------------------

ASSIGN_FORMAT = "athletrix-track-assignments"


@dataclass(frozen=True)
class Assignment:
    track_id: str
    athlete_id: str
    from_frame: int = 0
    to_frame: int | None = None   # inclusive; None = until the track ends
    assigned_by: str = ""


def load_assignments(path: Path) -> list[Assignment]:
    """{"format": "athletrix-track-assignments", "format_version": 1, "assignments": [{track_id, athlete_id, from_frame, to_frame, assigned_by}]}"""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != ASSIGN_FORMAT or doc.get("format_version") != 1 or not isinstance(doc.get("assignments"), list):
        raise ObservationError([f"{path}: expected format {ASSIGN_FORMAT!r} version 1 with an assignments list"])
    out, errors = [], []
    for n, a in enumerate(doc["assignments"], start=1):
        if not isinstance(a, dict) or not {"track_id", "athlete_id"} <= set(a) or not set(a) <= {"track_id", "athlete_id", "from_frame", "to_frame", "assigned_by"}:
            errors.append(f"assignment {n}: needs track_id and athlete_id (optional from_frame, to_frame, assigned_by)")
            continue
        if not all(isinstance(a[k], str) and a[k].strip() for k in ("track_id", "athlete_id")) or a["track_id"] == a["athlete_id"]:
            errors.append(f"assignment {n}: track_id and athlete_id must be distinct non-blank strings")
            continue
        lo, hi = a.get("from_frame", 0), a.get("to_frame")
        if not isinstance(lo, int) or lo < 0 or (hi is not None and (not isinstance(hi, int) or hi < lo)):
            errors.append(f"assignment {n}: frame range invalid")
            continue
        out.append(Assignment(a["track_id"], a["athlete_id"], lo, hi, str(a.get("assigned_by", ""))))
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if a.track_id == b.track_id and not (a.to_frame is not None and a.to_frame < b.from_frame or b.to_frame is not None and b.to_frame < a.from_frame):
                errors.append(f"track {a.track_id!r} has overlapping assignments")
    if errors:
        raise ObservationError(errors)
    return out


def apply_assignments(f: ObservationFile, assignments: list[Assignment]) -> ObservationFile:
    """Set athlete_id from explicit assignments. An observation already assigned differently is an error, never overwritten."""
    errors = []
    result = []
    for o in f.observations:
        hit = [a for a in assignments if a.track_id == o.track_id and a.from_frame <= o.frame and (a.to_frame is None or o.frame <= a.to_frame)]
        athlete = hit[0].athlete_id if hit else None
        if o.athlete_id is not None and athlete is not None and o.athlete_id != athlete:
            errors.append(f"frame {o.frame} track {o.track_id}: already assigned to {o.athlete_id}, assignment file says {athlete}")
            continue
        result.append(Observation(o.frame, o.video_time_s, o.track_id, o.u_px, o.v_px, o.confidence, o.athlete_id or athlete))
    clash = sorted({o.track_id for o in result} & {o.athlete_id for o in result if o.athlete_id})
    if clash:
        errors.append(f"track ids must differ from athlete ids: {', '.join(clash)}")
    if errors:
        raise ObservationError(errors)
    return ObservationFile(video=f.video, extractor=f.extractor, observations=result)
