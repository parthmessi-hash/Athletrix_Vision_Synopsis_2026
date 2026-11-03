"""Optional lens-distortion calibration on KNOWN synthetic geometry: recovery of a known lens, held-out views, corner
detection on rendered images, the ground homography with and without the lens model, resolution checks, version-1
files. Synthetic recovery is not measured accuracy of any physical camera."""

import json
import math

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from camera_adapter import calibration as cal  # noqa: E402
from camera_adapter import intrinsics as intr  # noqa: E402

W, H = 960, 540
K = [[700.0, 0, 480.0], [0, 700.0, 270.0], [0, 0, 1]]
D = [-0.32, 0.12, 0.001, -0.0005, 0.0]   # strong barrel distortion, like a wide sideline lens


def test_a_known_lens_is_recovered_and_checked_on_held_out_views():
    views = intr.synthetic_views(K, D, W, H, views=16)
    lens = intr.calibrate_from_points(views, W, H, cols=9, rows=6, square_mm=30.0, held_out_every=4, camera_id="synthetic-wide")
    assert lens.fit["views"] == 12 and lens.held_out["views"] == 4
    assert abs(lens.camera_matrix[0][0] - 700) < 10 and abs(lens.camera_matrix[0][2] - 480) < 10 and abs(lens.dist_coeffs[0] - D[0]) < 0.05
    assert lens.fit["rms_px"] < 0.3 and lens.held_out["rms_px"] < 0.4, "noise was 0.15 px; held-out views are checked with their own poses only"
    with pytest.raises(intr.IntrinsicsError, match="at least 3"):
        intr.calibrate_from_points(views[:3], W, H, cols=9, rows=6, square_mm=30.0, held_out_every=2)


def test_corners_are_found_on_a_rendered_distorted_image():
    rv, tv = np.array([0.2, -0.15, 0.05]), np.array([-120.0, -75.0, 600.0])
    img = intr.render_view(K, D, W, H, rv, tv)
    found = intr.detect_corners(img, 9, 6)
    truth, _ = cv2.projectPoints(intr.board_points(9, 6, 30.0), rv, tv, np.array(K), np.array(D))
    err = np.linalg.norm(found - truth.reshape(-1, 2), axis=1)
    assert found is not None and float(np.median(err)) < 0.5, "sub-pixel corners on the distorted rendering"
    assert intr.detect_corners(np.full((H, W), 200, np.uint8), 9, 6) is None


def ground_marks():
    """A camera above a sideline looking across a 40 x 20 m field, through the distorting lens: raw pixels of field marks."""
    rv, tv = cv2.Rodrigues(np.array([[1, 0, 0], [0, -0.42, -0.91], [0, 0.91, -0.42]]) @ np.eye(3))[0], None
    r, _ = cv2.Rodrigues(rv)
    cam = np.array([20.0, -9.0, 7.0])
    tv = -r @ cam
    def raw(x, y):
        p, _ = cv2.projectPoints(np.array([[x, y, 0.0]]), rv, tv, np.array(K), np.array(D))
        return [float(v) for v in p.reshape(2)]
    grid = [(x, y) for x in range(4, 37, 4) for y in range(2, 19, 4)]
    pts = [{"label": f"m{x}-{y}", "image": raw(x, y), "field": [x, y]} for x, y in grid]
    pts = [p for p in pts if 0 <= p["image"][0] < W and 0 <= p["image"][1] < H]
    return pts[::2], pts[1::2]


def test_the_ground_homography_with_the_lens_model_removes_distortion_error_and_keeps_points_raw(tmp_path):
    fit_pts, check_pts = ground_marks()
    assert len(fit_pts) >= 6 and len(check_pts) >= 6
    plain = cal.fit(W, H, fit_pts, check_pts, camera_id="synthetic-wide")
    lens = intr.calibrate_from_points(intr.synthetic_views(K, D, W, H, views=16), W, H, cols=9, rows=6, square_mm=30.0, camera_id="synthetic-wide")
    corrected = cal.fit(W, H, fit_pts, check_pts, camera_id="synthetic-wide", lens=lens)
    assert plain.check_errors_m["max"] > 0.5, "a homography alone cannot represent this distortion"
    assert corrected.check_errors_m["max"] < 0.1 < plain.check_errors_m["max"] / 5, (plain.check_errors_m["max"], corrected.check_errors_m["max"])
    path = tmp_path / "cal.json"
    corrected.save(path)
    doc = json.loads(path.read_text())
    assert doc["fit_points"][0]["image"] == fit_pts[0]["image"], "measured points stay raw in the file"
    assert doc["homography_input"].startswith("undistorted") and doc["lens"]["sha256"] == lens.sha256()
    again = cal.load(path)
    x, y = again.to_field(*check_pts[0]["image"])
    assert math.dist((x, y), check_pts[0]["field"]) < 0.1, "observations are undistorted the same way before mapping"
    doc["lens"]["dist_coeffs"][0] = -0.1
    path.write_text(json.dumps(doc))
    with pytest.raises(cal.CalibrationError, match="does not match its hash"):
        cal.load(path)
    with pytest.raises(cal.CalibrationError, match="valid only at its own resolution"):
        cal.fit(1280, 720, fit_pts, [], lens=lens)


def test_version_1_files_stay_readable_as_no_lens_model(tmp_path):
    fit_pts, _ = ground_marks()
    doc = cal.fit(W, H, fit_pts).to_doc()
    doc["format_version"] = 1
    for k in ("lens", "image_points", "homography_input"):
        doc.pop(k)
    (tmp_path / "v1.json").write_text(json.dumps(doc))
    old = cal.load(tmp_path / "v1.json")
    assert old.lens is None and old.to_doc()["homography_input"].startswith("raw pixels (no lens model")
