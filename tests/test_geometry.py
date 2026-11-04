from __future__ import annotations

import math

import numpy as np
import pytest

from screwpick.layout import Bin, Layout, yaw_from_up
from screwpick.parts import Part, compute_gaps, polygon_gap
from screwpick.transform import BedCalibration, Transform2D, fit_similarity


def test_fit_similarity_recovers_rotation_scale_shift():
    rng = np.random.default_rng(0)
    a = math.radians(1.7)
    A = 1.003 * np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    src = rng.uniform(0, 200, (6, 2))
    dst = src @ A.T + [12.5, -3.25]
    T, res = fit_similarity(src, dst)
    assert np.allclose(T.A, A, atol=1e-9) and np.allclose(T.t, [12.5, -3.25], atol=1e-7)
    assert res.max() < 1e-7
    assert T.angle_deg == pytest.approx(1.7) and T.scale == pytest.approx(1.003)
    back = T.inverse().apply(dst)
    assert np.allclose(back, src)


def test_fit_rejects_a_mirror_and_reports_residuals(tmp_path):
    pairs = [
        {"marker": 100, "plate": [0, 0], "bed": [10, 10]},
        {"marker": 101, "plate": [100, 0], "bed": [110, 10]},
        {"marker": 102, "plate": [0, 100], "bed": [10, 110.6]},  # 0.6 mm off
    ]
    cal = BedCalibration.fit(pairs)
    assert np.linalg.det(cal.transform.A) > 0
    assert 0.1 < max(cal.residuals_mm) < 0.6
    cal.save(tmp_path / "t.json")
    again = BedCalibration.load(tmp_path / "t.json")
    assert again is not None and np.allclose(again.transform.A, cal.transform.A)
    assert BedCalibration.load(tmp_path / "missing.json") is None


def test_layout_default_is_valid_and_round_trips(tmp_path):
    lay = Layout.default()
    assert lay.problems() == []
    assert len(lay.bins) == 17  # 16 around the 3x3 tray (the plate)
    assert lay.work_bin().marker == 220 and lay.work_bin().role == "work"
    lay.save(tmp_path / "layout.yaml")
    again = Layout.load(tmp_path / "layout.yaml")
    assert again.to_dict() == lay.to_dict()
    inset = 0.25 + 1.2  # half the gap between bins, the tray's wall
    x0, y0, x1, y1 = lay.work_rect()
    assert (x0, y0, x1) == pytest.approx((47 + inset, 47 + inset, 173 - inset))
    assert y1 == pytest.approx(173 - inset - 14.0)  # the marker shelf at the back is not plate
    assert lay.surface_z() == pytest.approx(4.75 + 1.25 + 1.75)  # tray floor + TPU sheet
    assert lay.in_work((110, 110)) and not lay.in_work((20, 20))


def test_layout_problems():
    lay = Layout.default()
    lay.bins.append(Bin(marker=100, x=2, y=2))  # duplicate marker, on the plate tray
    lay.bins.append(Bin(marker=300, x=9, y=0))  # bad id, off the grid
    lay.bins.append(Bin(marker=200, x=4, y=4, height_u=2, role="nope"))
    text = " | ".join(lay.problems())
    assert "used twice" in text and "overlaps bin 220" in text
    assert "outside the 5x5 grid" in text and "marker ids are" in text
    assert "height units" in text and "unknown role" in text


def test_place_from_marker_snaps_to_cells_and_turns():
    lay = Layout.default()
    b = lay.bin_by_marker(100)
    assert b is not None
    lay.place_from_marker(100, lay.bin_centre(Bin(marker=0, x=4, y=2)), yaw_deg=1.5)
    assert (b.x, b.y) == (4, 2)
    b.w, b.d = 2, 1
    nb = lay.place_from_marker(100, (5 + 42 * 3.5, 5 + 42 * 1.0), yaw_deg=91.0)
    assert (nb.w, nb.d) == (1, 2) and (nb.x, nb.y) == (3, 0)
    new = lay.place_from_marker(245, (5 + 21, 5 + 21), yaw_deg=0)  # a free id: 1x1
    assert lay.bin_by_marker(245) is new and (new.x, new.y) == (0, 0)
    assert yaw_from_up((0.0, 1.0)) == pytest.approx(0.0)
    assert yaw_from_up((-1.0, 0.0)) == pytest.approx(90.0)


def _square(cx: float, cy: float, s: float = 5.0) -> np.ndarray:
    return np.array([[cx - s, cy - s], [cx + s, cy - s], [cx + s, cy + s], [cx - s, cy + s]])


def test_polygon_gap_and_gaps():
    assert polygon_gap(_square(0, 0), _square(13, 0)) == pytest.approx(3.0)
    assert polygon_gap(_square(0, 0), _square(8, 0)) == 0.0  # overlapping
    assert polygon_gap(_square(0, 0), _square(0.5, 0.5, 1)) == 0.0  # one inside the other
    parts = [Part(i, "X", 0.9, (0, 0), 0, _square(cx, 0), 3) for i, cx in enumerate((0, 13, 100))]
    compute_gaps(parts)
    assert parts[0].gap_mm == pytest.approx(3.0) and parts[1].gap_mm == pytest.approx(3.0)
    assert math.isinf(parts[2].gap_mm)


def test_transform_identity_dict_round_trip():
    T = Transform2D.identity()
    assert np.allclose(Transform2D.from_dict(T.to_dict()).apply([[1, 2]]), [[1, 2]])
