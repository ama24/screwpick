"""Screen holder and camera arm for the printer's top bar."""

from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("manifold3d")
trimesh = pytest.importorskip("trimesh")

from sortering_pnp.printables.gridfinity import _to_mesh  # noqa: E402
from sortering_pnp.printables.mounts import (  # noqa: E402
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
    assert (p.frame_w, p.frame_h) == (168.0, 105.0)  # sized to the 155 x 92 holes, not the case
    assert hi[0] - lo[0] == pytest.approx(p.frame_w + 2 * p.foot_out, abs=0.1)  # feet outside
    assert lo[2] == pytest.approx(0.0, abs=0.05)  # stands on the bar's top
    top = p.bottom_z + (p.frame_y0 + p.frame_h) * math.cos(math.radians(20))  # leans back 20 deg
    assert hi[2] == pytest.approx(top, abs=0.1)
    m = trimesh.Trimesh(*build_screen_holder(p))
    assert m.is_watertight and m.body_count == 1
    assert m.bounds[1][0] <= 220 and m.bounds[1][1] <= 220  # fits an Ender 3 bed
    assert m.bounds[1][2] < 45  # lies flat: the frame on the bed, rim and legs rise


def _hole_centres(v: np.ndarray, r: float, near: list[tuple[float, float]], axes: tuple[int, int]):
    """For each expected centre: are there vertices on a circle of radius r around it?"""
    pts = v[:, list(axes)]
    found = []
    for c in near:
        d = np.linalg.norm(pts - np.array(c), axis=1)
        found.append(bool(np.any(np.abs(d - r) < 0.05)))
    return found


def test_screen_holes_and_feet():
    p = ScreenHolder(tilt_deg=0.0, lift=0.0)  # upright: x across, z up
    v, _ = _to_mesh(_screen_holder_installed(p))
    display = [(sx * 77.5, z) for sx in (-1, 1) for z in (11.5, 103.5)]  # 155 x 92 mm
    assert all(_hole_centres(v, 1.2, display, (0, 2)))
    feet = [(sx * p.foot_hole_dx / 2, p.foot_depth / 2) for sx in (-1, 1)]
    assert all(_hole_centres(v, 2.7, feet, (0, 1)))


@pytest.mark.parametrize("tilt", [0.0, 20.0, 35.0])
def test_the_pi_behind_the_display_has_room(tilt):
    """The Pi is screwed to the display's back, off centre: nothing of the holder may be
    anywhere behind the display except the band along its edge."""
    from sortering_pnp.printables.mounts import screen_open_space

    p = ScreenHolder(tilt_deg=tilt)
    clash = _screen_holder_installed(p) ^ screen_open_space(p)
    assert clash.volume() < 1e-3
    assert screen_open_space(p).volume() > 140 * 80 * 40  # the frame's opening is free
    assert screen_open_space(p).bounding_box()[2] > 2.9  # the Pi stays above the bar


@pytest.mark.parametrize("tilt", [0.0, 20.0, 35.0])
def test_the_display_overhangs_the_frame_without_touching_it(tilt):
    from sortering_pnp.printables.gridfinity import _box
    from sortering_pnp.printables.mounts import _to_installed

    p = ScreenHolder(tilt_deg=tilt)
    display = _box(-p.screen_w / 2, p.screen_w / 2, 0.0, p.screen_h, 0.01, 12.0)
    assert (_screen_holder_installed(p) ^ _to_installed(display, p)).volume() < 1e-3


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
    assert b"155 x 92" in files["MOUNTS.txt"]


def test_screen_screws_have_a_full_seat_for_their_heads():
    """155 x 92 mm holes, M2 screws with 4.8 mm heads: a ring of material all round each hole
    (from the hole out past the head) for the head to sit on."""
    import math as _m

    from manifold3d import Manifold

    p = ScreenHolder(tilt_deg=0.0, lift=0.0)
    body = _screen_holder_installed(p)
    t = p.thickness
    for sx in (-1, 1):
        for z in (11.5, 103.5):  # bottom and top holes (upright: z is up the screen)
            x = sx * 77.5
            ring = (
                (
                    Manifold.cylinder(t - 0.2, 2.6, 2.6, 48)
                    - Manifold.cylinder(t - 0.2, 1.4, 1.4, 48)
                )
                .rotate([90, 0, 0])
                .translate([x, t - 0.1, z])
            )
            want = _m.pi * (2.6**2 - 1.4**2) * (t - 0.2)
            assert (body ^ ring).volume() == pytest.approx(want, rel=0.03), (x, z)
    assert p.head_d >= 4.8 + 0.5


@pytest.mark.parametrize("reach", [135.0, 100.0])
def test_camera_arm_screws_go_in_from_the_front(reach):
    """M4 x 7 with a 7 mm head: a through hole, nothing in front of the head (it goes in and the
    screwdriver reaches it straight on), a full seat under it, and enough left for the T-nut."""
    from manifold3d import Manifold

    from sortering_pnp.printables.mounts import screw_seat

    p = CameraArm(reach=reach)
    body = _camera_arm_installed(p)
    seat = screw_seat(p)
    assert p.screw_len - seat >= 4.0  # into the slot: its lip and the T-nut's thread

    def along_y(r: float, y0: float, y1: float, x: float) -> Manifold:
        return Manifold.cylinder(y1 - y0, r, r, 48).rotate([-90, 0, 0]).translate([x, y0, -10.0])

    for x in (-p.screw_dx / 2, p.screw_dx / 2):
        shank = along_y(2.0, -seat - 0.1, 0.5, x)  # M4
        head_and_driver = along_y(3.5, -reach - 40.0, -seat - 0.05, x)  # 7 mm, straight on
        assert (body ^ shank).volume() < 1e-3, x
        assert (body ^ head_and_driver).volume() < 1e-3, x
        seat_ring = along_y(3.5, -seat + 0.05, -0.05, x) - along_y(2.4, -seat, 0.0, x)
        assert (body ^ seat_ring).volume() == pytest.approx(seat_ring.volume(), rel=0.02), x


def test_camera_arm_screw_length_is_checked():
    with pytest.raises(ValueError, match="mm long"):
        build_camera_arm(CameraArm(screw_len=16.0))


def test_camera_arm_is_straight_by_default():
    """No post: nothing goes lower than the camera's bosses under the arm, so the camera is as
    high as it can be and sees the most of the bed; one solid that prints flat."""
    p = CameraArm()
    v, _ = _to_mesh(_camera_arm_installed(p))
    assert v[:, 2].min() == pytest.approx(-p.bar - p.boss_h, abs=0.01)
    assert lens_position(p) == (0.0, -p.reach, -p.bar - p.boss_h)
    low = v[v[:, 2] < v[:, 2].min() + 0.01]
    assert low[:, 0].mean() == pytest.approx(0.0, abs=0.05)
    m = trimesh.Trimesh(*build_camera_arm(p))
    assert m.is_watertight and m.body_count == 1
    assert m.bounds[1][2] == pytest.approx(p.bar + p.boss_h, abs=0.01)  # lies flat, low
    assert b"straight" in dict(mount_files())["MOUNTS.txt"]
