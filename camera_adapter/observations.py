"""Camera observation contract (internal adapter format, version 2; version 1 is still read): what a frame-level
extractor hands to the adapter.

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
  ground_contact  (version 2, optional) the extractor's evidence that u_px, v_px really is where the target touches
                the ground, separate from `confidence`: "consistent" (nothing indicates otherwise), "suspect" (for
                example the box suddenly lost height, as when the legs are hidden) or "unknown" (not yet assessable).
                Absent means not assessed. A confident detection can still have a suspect ground point.
  near_other_track  (version 3, optional, written only when true) the target's box overlapped another tracked person's
                box in this frame, or did so within the extractor's release time (identity switches were seen to start
                a few frames after the boxes separate). A tracker can swap or merge identities in such an encounter, so an operator's
                assignment of the track lapses at it (see apply_assignments) until the track is confirmed again.

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
FORMAT_VERSION = 3
READABLE_VERSIONS = (1, 2, 3)
GROUND_CONTACT = ("consistent", "suspect", "unknown")
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
    ground_contact: str | None = None
    near_other_track: bool = False


@dataclass
class ObservationFile:
    video: Video
    extractor: str
    observations: list[Observation] = field(default_factory=list)
    assignment_report: dict | None = None     # set by apply_assignments: assignments that lapsed at an encounter


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
    if not isinstance(header, dict) or set(header) != HEADER_KEYS or header.get("format") != FORMAT or header.get("format_version") not in READABLE_VERSIONS:
        raise ObservationError([f"line 1: header must have exactly {sorted(HEADER_KEYS)} with format {FORMAT!r} version 1, 2 or 3"])
    version = header["format_version"]
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
        allowed = OBS_KEYS | ({"ground_contact"} if version >= 2 else set()) | ({"near_other_track"} if version >= 3 else set())
        if not isinstance(r, dict) or not OBS_KEYS <= set(r) <= allowed:
            errors.append(f"line {n}: an observation has exactly the keys {sorted(OBS_KEYS)}" + (f" and optionally {', '.join(sorted(allowed - OBS_KEYS))}" if version >= 2 else ""))
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
        if "ground_contact" in r and r["ground_contact"] not in GROUND_CONTACT:
            problems.append(f"ground_contact must be one of {', '.join(GROUND_CONTACT)}")
        if "near_other_track" in r and r["near_other_track"] is not True:
            problems.append("near_other_track is written only as true")
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
        obs.append(Observation(r["frame"], float(r["video_time_s"]), r["track_id"], float(r["u_px"]), float(r["v_px"]), float(r["confidence"]), r["athlete_id"], r.get("ground_contact"),
                               bool(r.get("near_other_track", False))))
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
    lines = [json.dumps(header)] + [json.dumps({k: v for k, v in asdict(o).items() if not (k == "ground_contact" and v is None) and not (k == "near_other_track" and not v)})
                                    for o in f.observations]
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
    identity: str = "confirmed"   # format 2: "uncertain" = recorded for audit, never used for an athlete's samples
    confirmed_by: str = "person"  # format 2: "person" or "simulated_oracle" (a script that reads the synthetic truth)


@dataclass
class AssignmentFile:
    """An assignments document with its version-2 extras (none for version 1)."""
    assignments: list[Assignment]
    video: dict | None = None           # {"name", "sha256"}: identifies the file the operator reviewed (not permission, not capture time)
    operator_work: dict | None = None   # what the operator did and how long it took (see load_assignment_file)


def load_assignments(path: Path) -> list[Assignment]:
    return load_assignment_file(path).assignments


ASSIGN_KEYS = {"track_id", "athlete_id", "from_frame", "to_frame", "assigned_by"}
WORK_KEYS = {"seconds_spent", "confirmations", "corrections", "uncertain_marks", "unassigned_seconds", "tool"}


def load_assignment_file(path: Path) -> AssignmentFile:
    """{"format": "athletrix-track-assignments", "format_version": 1 or 2, "assignments": [{track_id, athlete_id, from_frame,
    to_frame, assigned_by}]}. Version 2 adds per assignment `identity` ("confirmed" | "uncertain") and `confirmed_by`
    ("person" | "simulated_oracle"), and at the top `video` {"name", "sha256"} and `operator_work` {seconds_spent,
    confirmations, corrections, uncertain_marks, unassigned_seconds, tool}: the operator's real effort, kept with the file."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    version = doc.get("format_version") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("format") != ASSIGN_FORMAT or version not in (1, 2) or not isinstance(doc.get("assignments"), list):
        raise ObservationError([f"{path}: expected format {ASSIGN_FORMAT!r} version 1 or 2 with an assignments list"])
    top = {"format", "format_version", "assignments"} | ({"video", "operator_work"} if version == 2 else set())
    errors = [f"unexpected top-level keys: {', '.join(sorted(set(doc) - top))}"] if set(doc) - top else []
    keys = ASSIGN_KEYS | ({"identity", "confirmed_by"} if version == 2 else set())
    out = []
    for n, a in enumerate(doc["assignments"], start=1):
        if not isinstance(a, dict) or not {"track_id", "athlete_id"} <= set(a) or not set(a) <= keys:
            errors.append(f"assignment {n}: needs track_id and athlete_id (optional {', '.join(sorted(keys - {'track_id', 'athlete_id'}))})")
            continue
        if not all(isinstance(a[k], str) and a[k].strip() for k in ("track_id", "athlete_id")) or a["track_id"] == a["athlete_id"]:
            errors.append(f"assignment {n}: track_id and athlete_id must be distinct non-blank strings")
            continue
        lo, hi = a.get("from_frame", 0), a.get("to_frame")
        if not isinstance(lo, int) or lo < 0 or (hi is not None and (not isinstance(hi, int) or hi < lo)):
            errors.append(f"assignment {n}: frame range invalid")
            continue
        if a.get("identity", "confirmed") not in ("confirmed", "uncertain") or a.get("confirmed_by", "person") not in ("person", "simulated_oracle"):
            errors.append(f"assignment {n}: identity must be confirmed or uncertain, confirmed_by person or simulated_oracle")
            continue
        out.append(Assignment(a["track_id"], a["athlete_id"], lo, hi, str(a.get("assigned_by", "")), a.get("identity", "confirmed"), a.get("confirmed_by", "person")))
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if a.track_id == b.track_id and not (a.to_frame is not None and a.to_frame < b.from_frame or b.to_frame is not None and b.to_frame < a.from_frame):
                errors.append(f"track {a.track_id!r} has overlapping assignments")
    video, work = doc.get("video"), doc.get("operator_work")
    if video is not None and (not isinstance(video, dict) or set(video) != {"name", "sha256"} or not isinstance(video.get("sha256"), str) or len(video["sha256"]) != 64):
        errors.append("video must be {name, sha256 (64 hex characters)}")
    if work is not None and (not isinstance(work, dict) or not set(work) <= WORK_KEYS):
        errors.append(f"operator_work may only hold {', '.join(sorted(WORK_KEYS))}")
    if errors:
        raise ObservationError(errors)
    return AssignmentFile(out, video, work)


def dump_assignments(path: Path, f: AssignmentFile) -> None:
    doc = {"format": ASSIGN_FORMAT, "format_version": 2, "assignments": [asdict(a) for a in f.assignments]}
    if f.video:
        doc["video"] = f.video
    if f.operator_work:
        doc["operator_work"] = f.operator_work
    Path(path).write_text(json.dumps(doc, indent=2), encoding="utf-8")


def apply_assignments(f: ObservationFile, assignments: list[Assignment], *, trust_tracker_through_encounters: bool = False) -> ObservationFile:
    """Set athlete_id from explicit assignments. An observation already assigned differently is an error, never overwritten.

    Conservative identity rule (default): an assignment covers its track only up to the track's first close encounter
    with another tracked person after the assignment starts (an observation with near_other_track). From that frame on
    the observations stay unassigned, because the tracker may have swapped or merged the two people, until a new
    assignment for the track starts after the encounter (the operator confirmed who it is). `trust_tracker_through_encounters`
    keeps the earlier behaviour (assignments run through encounters). The report counts what lapsed."""
    errors = []
    result = []
    lapsed: dict[str, int] = {}
    uncertain: dict[str, int] = {}
    encounters: dict[str, list[int]] = {}
    for o in f.observations:
        if o.near_other_track:
            encounters.setdefault(o.track_id, []).append(o.frame)
    for o in f.observations:
        hit = [a for a in assignments if a.track_id == o.track_id and a.from_frame <= o.frame and (a.to_frame is None or o.frame <= a.to_frame)]
        athlete = hit[0].athlete_id if hit else None
        if hit and hit[0].identity == "uncertain":   # recorded for audit; an uncertain identity never feeds an athlete's samples
            uncertain[o.track_id] = uncertain.get(o.track_id, 0) + 1
            athlete = None
        if athlete is not None and not trust_tracker_through_encounters and any(hit[0].from_frame < e <= o.frame for e in encounters.get(o.track_id, ())):
            lapsed[o.track_id] = lapsed.get(o.track_id, 0) + 1
            athlete = None
        if o.athlete_id is not None and athlete is not None and o.athlete_id != athlete:
            errors.append(f"frame {o.frame} track {o.track_id}: already assigned to {o.athlete_id}, assignment file says {athlete}")
            continue
        result.append(Observation(o.frame, o.video_time_s, o.track_id, o.u_px, o.v_px, o.confidence, o.athlete_id or athlete, o.ground_contact, o.near_other_track))
    clash = sorted({o.track_id for o in result} & {o.athlete_id for o in result if o.athlete_id})
    if clash:
        errors.append(f"track ids must differ from athlete ids: {', '.join(clash)}")
    if errors:
        raise ObservationError(errors)
    report = {"rule": "trust_tracker_through_encounters" if trust_tracker_through_encounters else "lapse_at_encounter",
              "observations_unassigned_after_encounter": sum(lapsed.values()), "per_track": lapsed,
              "observations_with_uncertain_identity": sum(uncertain.values()), "uncertain_per_track": uncertain,
              "confirmed_by": sorted({a.confirmed_by for a in assignments}),
              "encounter_observations": {k: len(v) for k, v in encounters.items()}}
    return ObservationFile(video=f.video, extractor=f.extractor, observations=result, assignment_report=report)
