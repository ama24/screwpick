import math

import cv2
import numpy as np
import pytest

from sortering_pnp.calib import extrinsic, intrinsics
from sortering_pnp.calib.board import BoardSpec, detect, write_board_pdf
from sortering_pnp.calib.geometry import Camera, nominal_camera, roi_camera_matrix, roi_for_area
from synthetic_board import (
    intrinsic_views,
    make_sample,
)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    out = tmp_path_factory.mktemp("calib_sample")
    truth = make_sample(out, seed=1)
    return out, truth


def _undistort_field(K, dist, size, step=40):
    w, h = size
    pts = (
        np.stack(np.meshgrid(np.arange(0, w, step), np.arange(0, h, step)), -1)
        .reshape(-1, 1, 2)
        .astype(float)
    )
    return cv2.undistortPoints(pts, K, dist, P=K).reshape(-1, 2)


def test_intrinsics_recover_ground_truth(sample):
    out, truth = sample
    files = sorted((out / "intrinsics").glob("*.png"))
    res = intrinsics.calibrate(files, BoardSpec())
    K_true = np.asarray(truth["camera_matrix"])
    assert res.rms < 0.3
    assert res.K[0, 0] == pytest.approx(K_true[0, 0], rel=5e-3)
    assert res.K[1, 1] == pytest.approx(K_true[1, 1], rel=5e-3)
    assert abs(res.K[0, 2] - K_true[0, 2]) < 8 and abs(res.K[1, 2] - K_true[1, 2]) < 8
    # what matters: the undistortion maps agree wherever the board was observed
    f_est = _undistort_field(res.K, res.dist, res.image_size)
    f_true = _undistort_field(K_true, np.asarray(truth["dist_coeffs"]), res.image_size)
    w, h = res.image_size
    grid = (
        np.stack(np.meshgrid(np.arange(0, w, 40), np.arange(0, h, 40)), -1)
        .reshape(-1, 2)
        .astype(np.float32)
    )
    hull = cv2.convexHull(res.residuals[:, :2].astype(np.float32))
    inside = np.array(
        [cv2.pointPolygonTest(hull, (float(x), float(y)), False) >= 0 for x, y in grid]
    )
    assert inside.mean() > 0.6
    assert np.abs(f_est - f_true)[inside].max() < 0.5
    assert all(v.used for v in res.views)


def test_view_rejection(sample, tmp_path):
    out, _ = sample
    files = sorted((out / "intrinsics").glob("*.png"))[:6]
    blurry = tmp_path / "blurry.png"
    cv2.imwrite(str(blurry), cv2.GaussianBlur(cv2.imread(str(files[0])), (0, 0), 1.6))
    blank = tmp_path / "blank.png"
    cv2.imwrite(str(blank), np.full((960, 1280), 128, np.uint8))
    views, size = intrinsics.load_views([*files, blurry, blank], BoardSpec())
    used = intrinsics.select_views(views, 12, 0.4)
    reasons = {v.file: v.reason for v in views if not v.used}
    assert len(used) == 6
    assert "blurry" in reasons["blurry.png"] and "not found" in reasons["blank.png"]


def test_extrinsic_plate_frame(sample):
    out, truth = sample
    cam = Camera(
        np.asarray(truth["camera_matrix"]),
        np.asarray(truth["dist_coeffs"]),
        tuple(truth["image_size"]),
    )
    img = cv2.imread(str(out / "extrinsic" / "board_flat.png"))
    ext = extrinsic.solve(
        img,
        BoardSpec(),
        cam,
        thickness_mm=2.0,
        area={"origin_mm": [0, -105], "size_mm": [135, 105]},
    )
    assert ext["reprojection_rms_px"] < 0.4
    assert ext["camera_height_mm"] == pytest.approx(
        300 + 2.0, abs=0.3
    )  # z = 0 is 2 mm below the board
    T = np.asarray(ext["T_cam_plate"])
    assert np.allclose(T[:3, :3] @ T[:3, :3].T, np.eye(3), atol=1e-9)
    assert T[:3, 2] @ np.array([0, 0, -1.0]) > 0.99  # plate z points towards the camera
    full = Camera(cam.K, cam.dist, cam.image_size, T)
    # board corner (plate origin) projects onto the detected first chessboard corner
    d = detect(img, BoardSpec())
    first = d.corners[np.argmin(d.ids)]
    obj = d.object_points[np.argmin(d.ids)]
    proj = full.project(np.array([[obj[0], -obj[1], 2.0]]))[0]
    assert np.linalg.norm(proj - first) < 0.5
    roi = ext["roi"]
    assert roi["w"] % 32 == 0 and roi["h"] % 32 == 0
    assert ext["px_per_mm"] == pytest.approx(1100 / 302, rel=0.02)


def test_homography_and_parallax():
    cam = nominal_camera()
    pts = np.array([[10.0, 20.0, 0], [200.0, 150.0, 0], [110.0, 110.0, 0]])
    px = cam.project(pts)
    H = cam.homography()
    hp = (H @ np.c_[pts[:, :2], np.ones(3)].T).T
    assert np.allclose(hp[:, :2] / hp[:, 2:], px, atol=1e-6)
    back = cam.pixel_to_plate(px, 0.0)
    assert np.allclose(back, pts, atol=1e-6)
    high = np.array([[30.0, 40.0, 5.0]])
    assert np.allclose(cam.pixel_to_plate(cam.project(high), 5.0), high, atol=1e-6)
    assert cam.px_per_mm((110, 110)) == pytest.approx(4.74 / 1.4e-3 / 300, rel=1e-6)
    roi = roi_for_area(cam, (0, 0), (220, 220), 5)
    # 230 mm at 11.3 px/mm exceeds the 2592 px sensor height: the ROI is clipped to the image
    assert (
        roi["w"] % 32 == 0 and roi["h"] % 32 == 0 and 2560 <= roi["w"] <= 2624 and roi["h"] <= 2592
    )
    Kr = roi_camera_matrix(cam.K, roi)
    assert Kr[0, 2] == pytest.approx(cam.K[0, 2] - roi["x0"])


def test_board_pdf(tmp_path):
    write_board_pdf(BoardSpec(), tmp_path / "b.pdf", dpi=200)
    assert (tmp_path / "b.pdf").read_bytes()[:4] == b"%PDF"


def test_board_spec_measured_square():
    s = BoardSpec().with_square(17.9)  # measured squares of the printed 18 mm board
    assert s.square_mm == 17.9 and s.marker_mm == pytest.approx(13.5 * 17.9 / 18)


def test_intrinsic_views_cover_tilts():
    tilts = [math.degrees(math.acos(abs(T[2, 2]))) for T in intrinsic_views(14)]
    assert max(tilts) <= 22


def test_board_page_layout():
    from sortering_pnp.calib.board import BoardSpec, page_layout

    big = BoardSpec(11, 8, 18.0, 13.5, "DICT_5X5_100")  # the shipped board: A4 landscape
    (pw, ph), (x, y) = page_layout(big)
    assert (pw, ph) == (297.0, 210.0) and x == pytest.approx((297 - 198) / 2)
    small = BoardSpec(7, 5, 15.0, 11.0, "DICT_5X5_100")  # taller than wide fits portrait
    assert page_layout(small)[0] == (210.0, 297.0)
    with pytest.raises(ValueError):
        page_layout(BoardSpec(14, 10, 20.0, 15.0, "DICT_5X5_100"))  # 280 x 200 mm: too big for A4
