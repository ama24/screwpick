"""Screen holder and camera arm for the printer's top bar."""

from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("manifold3d")
trimesh = pytest.importorskip("trimesh")

from screwpick.printables.gridfinity import _to_mesh  # noqa: E402
from screwpick.printables.mounts import (  # noqa: E402
    CameraArm,
    ScreenHolder,
    _camera_arm_installed,
    _screen_holder_installed,
    build_camera_arm,
    build_screen_holder,
    drop_for,
    lens_position,
    mount_files,
)


def test_screen_holder_leans_back_and_prints_flat():
    p = ScreenHolder()
    v, _ = _to_mesh(_screen_holder_installed(p))
    lo, hi = v.min(axis=0), v.max(axis=0)
    assert hi[0] - lo[0] == pytest.approx(191.0, abs=0.1)  # as wide as the display
    assert lo[2] == pytest.approx(0.0, abs=0.05)  # sits on the bar's top
    assert hi[2] == pytest.approx(115 * math.cos(math.radians(20)), abs=1.5)  # leans back 20 deg
    assert hi[1] == pytest.approx(max(20.0, 115 * math.sin(math.radians(20))), abs=1.5)
    m = trimesh.Trimesh(*build_screen_holder(p))
    assert m.is_watertight
    assert m.bounds[1][0] <= 214 and m.bounds[1][1] <= 214  # fits an Ender 3 bed
    assert m.bounds[1][2] < 30  # lies flat: the upright on the bed, the foot rises a little


def _hole_centres(v: np.ndarray, r: float, near: list[tuple[float, float]], axes: tuple[int, int]):
    """For each expected centre: are there vertices on a circle of radius r around it?"""
    pts = v[:, list(axes)]
    found = []
    for c in near:
        d = np.linalg.norm(pts - np.array(c), axis=1)
        found.append(bool(np.any(np.abs(d - r) < 0.05)))
    return found


def test_screen_holes_match_the_display_and_the_pi():
    p = ScreenHolder(tilt_deg=0.0)  # upright: x across, z up
    v, _ = _to_mesh(_screen_holder_installed(p))
    display = [(sx * 87.5, z) for sx in (-1, 1) for z in (8.0, 107.0)]
    pi = [(sx * 24.5, z) for sx in (-1, 1) for z in (57.5 - 29.0, 57.5 + 29.0)]
    assert all(_hole_centres(v, 1.2, display, (0, 2)))
    assert all(_hole_centres(v, 1.4, pi, (0, 2)))


def test_camera_lens_over_the_bed_centre():
    p = CameraArm(reach=140.0, drop=50.0)
    v, _ = _to_mesh(_camera_arm_installed(p))
    low = v[v[:, 2] < v[:, 2].min() + 0.01]  # the boss ends: where the board lies
    cx, cy = low[:, 0].mean(), low[:, 1].mean()
    rows = np.array(p.cam_hole_rows)
    edge = -p.reach + p.lens_from_cable_edge
    assert cx == pytest.approx(0.0, abs=0.05)  # centred on the bar
    assert cy == pytest.approx(edge - rows.mean(), abs=0.1)
    assert lens_position(p)[:2] == (0.0, -140.0)  # lens straight below, reach mm forward
    assert v[:, 2].min() == pytest.approx(-20 - 50 - p.boss_h, abs=0.01)
    assert v[:, 1].max() == pytest.approx(0.0, abs=0.01)  # nothing behind the bar's front face
    m = trimesh.Trimesh(*build_camera_arm(p))
    assert m.is_watertight and m.bounds[0][2] == pytest.approx(0.0)


def test_drop_for_the_trained_camera_height():
    assert drop_for(364.0) == pytest.approx(60.0)
    with pytest.raises(ValueError, match="cannot be 300"):
        drop_for(250.0)


def test_mount_files():
    files = dict(mount_files())
    assert set(files) == {"screen_holder.stl", "camera_arm.stl", "MOUNTS.txt"}
    assert b"175 x 99" in files["MOUNTS.txt"]
