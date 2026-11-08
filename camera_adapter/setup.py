"""Operator checks for a physical camera setup: inspect a calibration, check that it fits the selected clip or camera
configuration, and say plainly what is untested. Pure reading; nothing is fitted or changed here.

A calibration is valid only for the camera, resolution and lens / zoom / focus / mount it was measured with. It is
INVALIDATED (blocked) when the stated resolution or camera differs, or when the operator-stated camera settings differ
from those recorded in the calibration; when the calibration does not record its settings, compatibility cannot be
confirmed (warning). Fitting residuals are not accuracy: only independent check points (not used in the fit) say how
far mapped positions were from measured ones, and only at those points.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CAPTURE_CHECKLIST = [
    "Fix the camera: mount, height, pan and tilt locked; note them. Any change after calibration invalidates it.",
    "Fix the lens: zoom, focus and any digital stabilisation or lens correction OFF or fixed; note the zoom position and the capture resolution and frame rate.",
    "Lens model (optional): 15-30 photos of a printed chessboard at THE SAME resolution and zoom, covering the corners and edges of the frame at several tilts; "
    "keep about one in four aside as held-out views (scripts/camera_intrinsics.py calibrate).",
    "Field reference points: at least 4 (better 6-10) pitch points measured in metres in one field coordinate system (line intersections, penalty spots, "
    "cones measured with a tape), spread across the area players use, with their pixel positions read on a still frame.",
    "Separate check points: at least 3 further measured points NOT used for fitting, spread across the area, to report independent error.",
    "Record camera_settings in the points file (camera_id, resolution, zoom, focus, lens, mount note) so a later change can be detected.",
    "Clock: note the camera's clock against a reference (e.g. film a phone showing UTC, or a clap / whistle seen by every stream) at the start AND end "
    "of the session, for scripts/time_align.py.",
]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_calibration(path: Path) -> dict:
    """Readable summary of a calibration file (re-validated by camera_adapter.calibration.load)."""
    from camera_adapter import calibration as cal  # noqa: PLC0415
    path = Path(path)
    try:
        c = cal.load(path)
    except cal.CalibrationError as exc:
        return {"path": path.name, "valid": False, "errors": exc.errors if hasattr(exc, "errors") else [str(exc)]}
    except (OSError, ValueError, ImportError) as exc:
        return {"path": path.name, "valid": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    doc = json.loads(path.read_text(encoding="utf-8"))
    lens = None
    if c.lens is not None:
        lens = {"camera_id": c.lens.camera_id, "resolution": [c.lens.width, c.lens.height], "sha256": c.lens.sha256()[:16],
                "fit_rms_px": (c.lens.fit or {}).get("rms_px"), "held_out_rms_px": (c.lens.held_out or {}).get("rms_px")}
    chk = c.check_errors_m
    return {"path": path.name, "sha256": file_sha256(path), "valid": True, "format_version": doc.get("format_version"), "camera_id": c.camera_id or None,
            "resolution": [c.width, c.height], "created_at": c.created_at or None, "camera_settings": c.camera_settings, "lens_model": lens,
            "fit_points": len(c.fit_points), "check_points": len(c.check_points),
            "fit_residual_m": {"rms": round(c.fit_residual_m["rms"], 3), "max": round(c.fit_residual_m["max"], 3),
                               "meaning": "residual of the points used for fitting; NOT independent accuracy"},
            "check_error_m": None if not chk else {"rms": round(chk["rms"], 3), "max": round(chk["max"], 3), "points": len(c.check_points),
                                                    "meaning": "error at measured points not used for fitting: independent, but only at those points"},
            "untested": ["accuracy away from the check points", "players' foot position on real footage", "any change to camera, lens, zoom or mount after calibration"]
                        + ([] if chk else ["independent accuracy: no check points were given"])}


def compatibility(summary: dict, target: dict | None) -> list[dict]:
    """[{level, text}] for using this calibration with `target` ({width, height, camera_id, camera_settings})."""
    out = []
    if not summary.get("valid"):
        return [{"level": "block", "text": "calibration invalid: " + "; ".join(summary.get("errors", []))}]
    t = target or {}
    w, h = summary["resolution"]
    if t.get("width") and t.get("height"):
        if [t["width"], t["height"]] != [w, h]:
            out.append({"level": "block", "text": f"resolution {t['width']}x{t['height']} differs from the calibration's {w}x{h}: invalid for this clip/camera"})
        else:
            out.append({"level": "ok", "text": f"resolution {w}x{h} matches"})
    else:
        out.append({"level": "warn", "text": "the clip/camera resolution is not stated: compatibility cannot be confirmed"})
    if t.get("camera_id") and summary.get("camera_id") and t["camera_id"] != summary["camera_id"]:
        out.append({"level": "block", "text": f"camera {t['camera_id']!r} is not the calibrated camera {summary['camera_id']!r}"})
    lens = summary.get("lens_model")
    if lens and lens["resolution"] != [w, h]:
        out.append({"level": "block", "text": "the embedded lens model was calibrated at another resolution"})
    cs, ts = summary.get("camera_settings"), t.get("camera_settings")
    if ts:
        if not cs:
            out.append({"level": "warn", "text": "the calibration does not record its camera settings: cannot confirm lens/zoom/mount are unchanged"})
        else:
            changed = sorted(k for k in set(cs) | set(ts) if cs.get(k) != ts.get(k))
            out.append({"level": "block", "text": "camera settings changed since calibration (" + ", ".join(f"{k}: {cs.get(k)!r} -> {ts.get(k)!r}" for k in changed) + "): recalibrate"}
                       if changed else {"level": "ok", "text": "stated camera settings match the calibration"})
    else:
        out.append({"level": "warn", "text": "current camera settings not stated: a lens, zoom or mount change would go unnoticed"})
    if not summary.get("check_error_m"):
        out.append({"level": "warn", "text": "no independent check points: positional accuracy is unknown"})
    else:
        c = summary["check_error_m"]
        out.append({"level": "ok", "text": f"independent check points: RMS {c['rms']} m, max {c['max']} m at {c['points']} points (fit residual RMS {summary['fit_residual_m']['rms']} m is not accuracy)"})
    return out


def camera_checks(camera_plan: dict) -> list[dict]:
    """Local checks for the session tool: calibration and time mapping files named in the plan's camera source."""
    out = []
    path = camera_plan.get("calibration_path")
    if not path:
        out.append({"level": "warn", "text": "no calibration file named: field positions cannot be calculated (raw recording unaffected)"})
    elif not Path(path).is_file():
        out.append({"level": "block", "text": f"calibration file not found: {path}"})
    else:
        s = inspect_calibration(Path(path))
        out += compatibility(s, camera_plan.get("clip_or_camera"))
    tm = camera_plan.get("time_mapping_path")
    if not tm:
        out.append({"level": "warn", "text": "no time alignment named: camera and wearable timelines stay independent; cross-stream comparison is unavailable"})
    elif not Path(tm).is_file():
        out.append({"level": "block", "text": f"time alignment file not found: {tm}"})
    else:
        from app.core.alignment import AlignmentError, describe, load  # noqa: PLC0415
        try:
            a = load(Path(tm))
            out.append({"level": "ok" if a["basis"] != "replay_anchor" else "warn", "text": describe(a)})
        except AlignmentError as exc:
            out.append({"level": "block", "text": f"time alignment invalid: {exc}"})
    out.append({"level": "warn", "text": "physical calibration and timing are untested until a real camera and an approved clip are checked"})
    return out
