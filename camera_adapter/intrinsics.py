"""Optional camera intrinsics and lens distortion (OpenCV pinhole model with k1, k2, p1, p2, k3). Needs OpenCV and NumPy.

Calibrated from views of a flat chessboard of known geometry. Views are split: most fit the model, and every n-th view
is HELD OUT. For a held-out view only the board's pose is estimated (solvePnP with the fitted intrinsics); its
reprojection error is therefore a check of the intrinsics on views they were not fitted to, not a measure of
on-field accuracy. Fit RMS alone is never treated as acceptance, and no universal threshold is applied.

COORDINATES. Raw image points are pixels of the full frame as captured (OpenCV convention, (0, 0) the centre of the
top-left pixel). Undistorted points are in the same pixel frame after removing the modelled distortion
(cv2.undistortPoints with P = the camera matrix). The ground homography (camera_adapter/calibration.py) is fitted to
undistorted points when a lens model is attached, and every observation is undistorted the same way before mapping.
A lens model is only valid at the resolution it was calibrated at; any other resolution is refused.

FILE (format "athletrix-camera-intrinsics", version 1): camera_id, image {width, height}, target, model, camera_matrix,
dist_coeffs, fit {views, rms_px, per_view_rms_px}, held_out {views, rms_px, per_view_rms_px} (or null), assumptions,
recalibrate_when, created_at, software.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FORMAT = "athletrix-camera-intrinsics"
FORMAT_VERSION = 1
MODEL = "opencv pinhole, radial k1 k2 k3 and tangential p1 p2 (cv2.calibrateCamera)"
ASSUMPTIONS = [
    "the lens, focus and zoom are the same as when the views were taken; the model is valid only at this resolution",
    "the chessboard was flat and its square size is as stated",
    "fit RMS is not independent accuracy; held-out views check the intrinsics only, not positions on the field",
    "no universal acceptable-error threshold is implied",
]
RECALIBRATE_WHEN = ["zoom or focus changed", "a different lens or camera body", "a different capture resolution or a cropped stream",
                    "the camera's internal stabilisation or lens-correction settings changed"]


class IntrinsicsError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Intrinsics:
    camera_id: str
    width: int
    height: int
    camera_matrix: list[list[float]]
    dist_coeffs: list[float]
    target: dict
    fit: dict
    held_out: dict | None
    created_at: str = ""
    software: dict | None = None

    def to_doc(self) -> dict:
        return {"format": FORMAT, "format_version": FORMAT_VERSION, "camera_id": self.camera_id, "image": {"width": self.width, "height": self.height},
                "target": self.target, "model": MODEL, "camera_matrix": self.camera_matrix, "dist_coeffs": self.dist_coeffs, "fit": self.fit,
                "held_out": self.held_out, "assumptions": ASSUMPTIONS, "recalibrate_when": RECALIBRATE_WHEN, "created_at": self.created_at,
                "software": self.software or {}}

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps({"K": self.camera_matrix, "d": self.dist_coeffs, "size": [self.width, self.height]}, sort_keys=True).encode()).hexdigest()

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_doc(), indent=2) + "\n", encoding="utf-8")

    def undistort(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Raw pixel points -> undistorted pixel points in the same frame."""
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        if not points:
            return []
        k, d = np.array(self.camera_matrix, float), np.array(self.dist_coeffs, float)
        out = cv2.undistortPoints(np.array(points, float).reshape(-1, 1, 2), k, d, P=k)
        return [(float(u), float(v)) for u, v in out.reshape(-1, 2)]


def board_points(cols: int, rows: int, square: float):
    import numpy as np  # noqa: PLC0415
    g = np.zeros((cols * rows, 3), np.float32)
    g[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square
    return g


def calibrate_from_points(views: list, width: int, height: int, *, cols: int, rows: int, square_mm: float, held_out_every: int = 4,
                          camera_id: str = "", software: dict | None = None) -> Intrinsics:
    """Fit from detected inner-corner points, one (cols*rows, 2) array per view; every n-th view is held out (0 = none)."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    obj = board_points(cols, rows, square_mm)
    held = [i for i in range(len(views)) if held_out_every and i % held_out_every == held_out_every - 1]
    fit_ids = [i for i in range(len(views)) if i not in held]
    if len(fit_ids) < 3:
        raise IntrinsicsError([f"{len(fit_ids)} fitting views; at least 3 are needed (more, at varied angles, is better)"])
    pts = [np.asarray(views[i], np.float32).reshape(-1, 1, 2) for i in fit_ids]
    rms, k, d, rvecs, tvecs = cv2.calibrateCamera([obj] * len(pts), pts, (width, height), None, None)
    if not (np.all(np.isfinite(k)) and np.all(np.isfinite(d))):
        raise IntrinsicsError(["the fit did not converge to finite parameters"])

    def view_rms(p, rv, tv):
        proj, _ = cv2.projectPoints(obj, rv, tv, k, d)
        return float(math.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - np.asarray(p, float).reshape(-1, 2)) ** 2, axis=1))))
    per_fit = [view_rms(views[i], rv, tv) for i, rv, tv in zip(fit_ids, rvecs, tvecs)]
    held_doc = None
    if held:
        per = []
        for i in held:
            ok, rv, tv = cv2.solvePnP(obj, np.asarray(views[i], np.float32).reshape(-1, 1, 2), k, d)
            per.append(view_rms(views[i], rv, tv) if ok else float("nan"))
        finite = [e for e in per if math.isfinite(e)]
        held_doc = {"views": len(held), "rms_px": round(math.sqrt(sum(e * e for e in finite) / len(finite)), 4) if finite else None, "per_view_rms_px": [round(e, 4) for e in per],
                    "note": "views not used to fit the intrinsics; only each view's board pose was estimated. Checks the lens model, not field positions."}
    return Intrinsics(camera_id, width, height, [[float(v) for v in row] for row in k], [float(v) for v in d.reshape(-1)[:5]],
                      {"type": "chessboard", "inner_corners": [cols, rows], "square_size_mm": square_mm},
                      {"views": len(fit_ids), "rms_px": round(float(rms), 4), "per_view_rms_px": [round(e, 4) for e in per_fit],
                       "note": "reprojection residual of the views used in the fit; not independent accuracy"},
                      held_doc, datetime.now(UTC).isoformat().replace("+00:00", "Z"), {"method": "cv2.calibrateCamera", **(software or {})})


def detect_corners(image, cols: int, rows: int):
    """Inner chessboard corners with sub-pixel refinement, or None when the board is not found."""
    import cv2  # noqa: PLC0415
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    found, corners = cv2.findChessboardCorners(gray, (cols, rows), cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not found:
        return None
    return cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-4)).reshape(-1, 2)


def load(path: Path) -> Intrinsics:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("format_version") != FORMAT_VERSION:
        raise IntrinsicsError([f"{path}: not an {FORMAT} version {FORMAT_VERSION} file"])
    return from_doc(doc)


def from_doc(doc: dict) -> Intrinsics:
    try:
        k, d, img = doc["camera_matrix"], doc["dist_coeffs"], doc["image"]
        ok = len(k) == 3 and all(len(r) == 3 for r in k) and len(d) == 5 and all(math.isfinite(float(v)) for v in [*sum(k, []), *d])
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok or not (isinstance(img.get("width"), int) and isinstance(img.get("height"), int)):
        raise IntrinsicsError(["camera_matrix must be 3x3, dist_coeffs five finite numbers, image {width, height} integers"])
    return Intrinsics(doc.get("camera_id", ""), img["width"], img["height"], k, d, doc.get("target", {}), doc.get("fit", {}), doc.get("held_out"),
                      doc.get("created_at", ""), doc.get("software"))


# ---- synthetic views with known distortion (for tests and demonstrations only) ----------------------------------------------

def synthetic_views(k, d, width: int, height: int, *, cols: int = 9, rows: int = 6, square: float = 30.0, views: int = 16, noise_px: float = 0.15, seed: int = 7):
    """Exact corner positions of a chessboard seen from varied poses through a known lens, plus pixel noise."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    rng = np.random.default_rng(seed)
    obj = board_points(cols, rows, square)
    out = []
    centre = np.array([(cols - 1) * square / 2, (rows - 1) * square / 2, 0.0])
    while len(out) < views:
        rv = rng.uniform([-0.6, -0.6, -0.3], [0.6, 0.6, 0.3])
        r, _ = cv2.Rodrigues(rv)
        tv = -r @ centre + np.array([rng.uniform(-120, 120), rng.uniform(-80, 80), rng.uniform(450, 800)])
        p, _ = cv2.projectPoints(obj, rv, tv, np.array(k, float), np.array(d, float))
        p = p.reshape(-1, 2)
        if p[:, 0].min() < 5 or p[:, 1].min() < 5 or p[:, 0].max() > width - 6 or p[:, 1].max() > height - 6:
            continue
        out.append(p + rng.normal(0, noise_px, p.shape))
    return out


def render_view(k, d, width: int, height: int, rvec, tvec, *, cols: int = 9, rows: int = 6, square: float = 30.0):
    """A distorted image of a chessboard (for exercising corner detection); 8-bit grey."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    k, d = np.array(k, float), np.array(d, float)
    px = 8   # texture pixels per mm-square unit
    tex = np.full(((rows + 3) * px * 4, (cols + 3) * px * 4), 255, np.uint8)
    s = px * 4
    for i in range(cols + 1):
        for j in range(rows + 1):
            if (i + j) % 2 == 0:
                tex[(j + 1) * s:(j + 2) * s, (i + 1) * s:(i + 2) * s] = 0
    tw, th = tex.shape[1], tex.shape[0]   # pixel-centre convention: the texture's outer edges lie half a pixel outside its first and last centres
    corners_tex = np.float32([[-0.5, -0.5], [tw - 0.5, -0.5], [tw - 0.5, th - 0.5], [-0.5, th - 0.5]])
    world = np.float32([[-2 * square, -2 * square, 0], [(cols + 1) * square, -2 * square, 0], [(cols + 1) * square, (rows + 1) * square, 0], [-2 * square, (rows + 1) * square, 0]])
    ideal, _ = cv2.projectPoints(world, rvec, tvec, k, np.zeros(5))
    h = cv2.getPerspectiveTransform(corners_tex, ideal.reshape(-1, 2).astype(np.float32))
    undistorted = cv2.warpPerspective(tex, h, (width, height), borderValue=200)
    grid = np.stack(np.meshgrid(np.arange(width), np.arange(height)), -1).reshape(-1, 1, 2).astype(np.float64)
    src = cv2.undistortPoints(grid, k, d, P=k).reshape(height, width, 2).astype(np.float32)   # for each distorted pixel, where it lies undistorted
    return cv2.remap(undistorted, src[..., 0], src[..., 1], cv2.INTER_LINEAR, borderValue=200)
