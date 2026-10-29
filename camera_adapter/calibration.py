"""Planar field calibration: image pixels -> ground-plane metres through a homography fitted to measured points.

Method: normalised direct linear transform (Hartley normalisation of both point sets, then a linear least-squares solve
for the eight free entries with h33 = 1, de-normalised). Plain Python, no NumPy or OpenCV needed; the synthetic check
cross-checks it against OpenCV's findHomography when OpenCV is installed.

Assumptions (stated in every calibration file): a fixed camera (moving, zooming or refocusing it requires a new
calibration), an approximately planar ground, no lens distortion correction (a wide-angle lens bends straight lines and
this model cannot represent that), and image points measured in the same full-resolution frame as the observations.

Fitting points determine the transformation; their residual is NOT independent accuracy. Check points are never used
in the fit, and their errors in metres are the only independent figure this file reports. No universal acceptable-error
threshold is applied: what is acceptable depends on the use and must be decided and recorded by the people using it.

FILE (format "athletrix-camera-calibration", version 1): see `Calibration.to_doc`.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

FORMAT = "athletrix-camera-calibration"
FORMAT_VERSION = 1
METHOD = "normalised DLT, linear least squares, h33 = 1 (camera_adapter/calibration.py)"
DUPLICATE_IMAGE_PX = 0.5
DUPLICATE_FIELD_M = 0.01
COLLINEAR_RELATIVE_AREA = 1e-3   # a triangle smaller than this fraction of the squared extent counts as collinear
CONVENTIONS = {
    "image": "pixels of the full frame, OpenCV convention: (0, 0) is the centre of the top-left pixel, u to the right, v downwards",
    "field": "metres on the ground plane; origin and axes are those of the measured field points (for example x along one sideline, y across); height is ignored",
    "transformation": "image_to_field maps homogeneous (u, v, 1) to (x*w, y*w, w); divide by w",
}
ASSUMPTIONS = [
    "fixed camera: moving, zooming or refocusing it requires a new calibration",
    "approximately planar ground; points off the plane (a raised ball, a head) are not mapped correctly",
    "no lens-distortion model: wide-angle distortion is not corrected",
    "fitting residual is not independent accuracy; only check points that were not used in the fit say anything independent",
    "no universal acceptable-error threshold is implied",
]


class CalibrationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# ---- small linear algebra --------------------------------------------------------------------------------------------------

def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(b[0]))] for i in range(len(a))]


def inv3(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if det == 0 or not math.isfinite(det):
        raise CalibrationError(["the transformation is singular"])
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def det3(m) -> float:
    (a, b, c), (d, e, f), (g, h, i) = m
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; raises CalibrationError on a (near-)singular system."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    scale = max(abs(x) for row in a for x in row) or 1.0
    for col in range(n):
        p = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[p][col]) < 1e-12 * scale:
            raise CalibrationError(["the point arrangement does not determine a transformation (singular system)"])
        m[col], m[p] = m[p], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for k in range(col, n + 1):
                m[r][k] -= f * m[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def normaliser(points):
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    d = sum(math.hypot(p[0] - cx, p[1] - cy) for p in points) / len(points)
    s = math.sqrt(2) / d if d > 0 else 1.0
    return [[s, 0, -s * cx], [0, s, -s * cy], [0, 0, 1]]


def apply_h(h, x: float, y: float) -> tuple[float, float, float]:
    """(X, Y, w) with X, Y already divided by w; w keeps its sign so a caller can reject points beyond the horizon."""
    px = h[0][0] * x + h[0][1] * y + h[0][2]
    py = h[1][0] * x + h[1][1] * y + h[1][2]
    w = h[2][0] * x + h[2][1] * y + h[2][2]
    return (px / w, py / w, w) if w != 0 else (math.nan, math.nan, 0.0)


def fit_homography(src, dst):
    """3x3 H with dst ~ H src, from >= 4 correspondences (normalised DLT, least squares)."""
    ts, td = normaliser(src), normaliser(dst)
    s2 = [apply_h(ts, *p)[:2] for p in src]
    d2 = [apply_h(td, *p)[:2] for p in dst]
    rows, rhs = [], []
    for (x, y), (u, v) in zip(s2, d2):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y]); rhs.append(v)
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(8)] for i in range(8)]
    atb = [sum(r[i] * b for r, b in zip(rows, rhs)) for i in range(8)]
    h = solve(ata, atb)
    hn = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]]
    full = matmul(inv3(td), matmul(hn, ts))
    k = full[2][2] if full[2][2] != 0 else 1.0
    return [[v / k for v in row] for row in full]


# ---- validation -------------------------------------------------------------------------------------------------------------

def _finite_pair(p) -> bool:
    return isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p)


def _general_four(points) -> bool:
    """True when some four points have no three (nearly) collinear, relative to the set's extent."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    if extent <= 0:
        return False
    tol = COLLINEAR_RELATIVE_AREA * extent * extent

    def area(a, b, c):
        return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2

    for quad in itertools.combinations(points, 4):
        if all(area(*tri) > tol for tri in itertools.combinations(quad, 3)):
            return True
    return False


def validate_points(points: list[dict], width: int, height: int, what: str, minimum: int) -> list[str]:
    errors = []
    if len(points) < minimum:
        errors.append(f"{what}: at least {minimum} correspondences are needed, got {len(points)}")
    for n, p in enumerate(points, start=1):
        if not isinstance(p, dict) or not _finite_pair(p.get("image")) or not _finite_pair(p.get("field")):
            errors.append(f"{what} {n}: needs finite image [u, v] in pixels and field [x, y] in metres")
            continue
        u, v = p["image"]
        if not (-0.5 <= u <= width - 0.5 and -0.5 <= v <= height - 0.5):
            errors.append(f"{what} {n}: image point ({u}, {v}) is outside the {width}x{height} frame")
    if errors:
        return errors
    for kind, tol, unit in (("image", DUPLICATE_IMAGE_PX, "px"), ("field", DUPLICATE_FIELD_M, "m")):
        for (i, a), (j, b) in itertools.combinations(enumerate(points, start=1), 2):
            if math.dist(a[kind], b[kind]) < tol:
                errors.append(f"{what} {i} and {j}: {kind} points coincide (within {tol} {unit})")
    if not errors and minimum >= 4:
        for kind in ("image", "field"):
            if not _general_four([p[kind] for p in points]):
                errors.append(f"{what}: the {kind} points are degenerate (no four of them with three not collinear)")
    return errors


# ---- the calibration ---------------------------------------------------------------------------------------------------------

@dataclass
class Calibration:
    width: int
    height: int
    fit_points: list[dict]
    check_points: list[dict]
    image_to_field: list[list[float]]
    camera_id: str = ""
    created_at: str = ""
    fit_residual_m: dict = field(default_factory=dict)
    check_errors_m: dict | None = None
    software: dict = field(default_factory=dict)

    @property
    def front_sign(self) -> float:
        return math.copysign(1.0, apply_h(self.image_to_field, *self.fit_points[0]["image"])[2])

    def to_field(self, u: float, v: float) -> tuple[float, float] | None:
        """Field position of an image point, or None when it maps beyond the horizon (the opposite side of w)."""
        x, y, w = apply_h(self.image_to_field, u, v)
        if w == 0 or math.copysign(1.0, w) != self.front_sign or not (math.isfinite(x) and math.isfinite(y)):
            return None
        return x, y

    def field_bounds(self) -> tuple[float, float, float, float]:
        xs = [p["field"][0] for p in self.fit_points + self.check_points]
        ys = [p["field"][1] for p in self.fit_points + self.check_points]
        return min(xs), min(ys), max(xs), max(ys)

    def to_doc(self) -> dict:
        return {"format": FORMAT, "format_version": FORMAT_VERSION, "created_at": self.created_at, "camera_id": self.camera_id,
                "image": {"width": self.width, "height": self.height}, "conventions": CONVENTIONS,
                "fit_points": self.fit_points, "check_points": self.check_points, "image_to_field": self.image_to_field,
                "fit_residual_m": self.fit_residual_m, "check_errors_m": self.check_errors_m,
                "assumptions": ASSUMPTIONS, "software": self.software}

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_doc(), indent=2) + "\n", encoding="utf-8")


def _errors(h, points) -> dict:
    per = []
    for p in points:
        x, y, w = apply_h(h, *p["image"])
        per.append({"label": p.get("label", ""), "error_m": math.dist((x, y), p["field"])})
    errs = [e["error_m"] for e in per]
    return {"per_point": per, "rms": math.sqrt(sum(e * e for e in errs) / len(errs)), "max": max(errs)}


def fit(width: int, height: int, fit_points: list[dict], check_points: list[dict] | None = None, *, camera_id: str = "", software: dict | None = None) -> Calibration:
    check_points = check_points or []
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        raise CalibrationError(["image width and height must be positive integers"])
    errors = validate_points(fit_points, width, height, "fit point", 4) + validate_points(check_points, width, height, "check point", 0)
    if not errors:
        fit_set = {tuple(p["field"]) for p in fit_points}
        if any(tuple(p["field"]) in fit_set for p in check_points):
            errors.append("check points must not repeat fitting points (they would not be independent)")
    if errors:
        raise CalibrationError(errors)
    h = fit_homography([p["image"] for p in fit_points], [p["field"] for p in fit_points])
    if not all(math.isfinite(v) for row in h for v in row) or abs(det3(h)) < 1e-15:
        raise CalibrationError(["the fitted transformation is unusable (singular or not finite)"])
    signs = {math.copysign(1.0, apply_h(h, *p["image"])[2]) for p in fit_points + check_points}
    if len(signs) != 1:
        raise CalibrationError(["the points lie on both sides of the image horizon for this transformation; the arrangement or the measurements are inconsistent"])
    cal = Calibration(width, height, [dict(p) for p in fit_points], [dict(p) for p in check_points], h, camera_id=camera_id,
                      created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                      software={"method": METHOD, **(software or {})})
    cal.fit_residual_m = {**_errors(h, fit_points), "note": "residual of the points used for fitting; not independent accuracy"}
    cal.check_errors_m = ({**_errors(h, check_points), "note": "points not used in the fit: independent check of this calibration only"} if check_points else None)
    return cal


def load(path: Path) -> Calibration:
    """Read and re-validate a calibration file; the stored transformation is refitted and must agree (detects edits)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("format_version") != FORMAT_VERSION:
        raise CalibrationError([f"{path}: not an {FORMAT} version {FORMAT_VERSION} file"])
    try:
        img = doc["image"]
        cal = fit(img["width"], img["height"], doc["fit_points"], doc.get("check_points") or [], camera_id=doc.get("camera_id", ""), software=doc.get("software"))
    except (KeyError, TypeError) as exc:
        raise CalibrationError([f"{path}: missing or malformed field ({exc})"]) from exc
    stored = doc.get("image_to_field")
    probe = [p["image"] for p in doc["fit_points"]]
    if not isinstance(stored, list) or any(math.dist(apply_h(stored, *q)[:2], cal.to_field(*q) or (math.inf, math.inf)) > 1e-6 for q in probe):
        raise CalibrationError([f"{path}: the stored transformation does not match its fitting points (edited or corrupted file)"])
    cal.created_at = doc.get("created_at", "")
    return cal
