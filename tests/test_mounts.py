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
    assert v[:, 2].min() == pytest.approx(-p.head_h - 50 - p.boss_h, abs=0.01)
    assert v[:, 1].max() == pytest.approx(0.0, abs=0.01)  # nothing behind the bar's front face
    m = trimesh.Trimesh(*build_camera_arm(p))
    assert m.is_watertight and m.body_count == 1 and m.bounds[0][2] == pytest.approx(0.0)


def test_drop_for_the_trained_camera_height():
    p = CameraArm()
    drop = drop_for(364.0)
    assert drop == pytest.approx(364 + p.bar - p.head_h - p.boss_h - 300)
    lens_above_plate = 364.0 + p.bar + lens_position(CameraArm(drop=drop))[2]
    assert lens_above_plate == pytest.approx(300.0)
    with pytest.raises(ValueError, match="cannot be 300"):
        drop_for(250.0)


def test_mount_files():
    files = dict(mount_files())
    assert set(files) == {
        "screen_holder.stl",
        "camera_arm.stl",
        "camera_arm_ring.stl",
        "ring_bracket.stl",
        "ring_clamp.stl",
        "ring_diffuser.stl",
        "MOUNTS.txt",
    }
    notes = " ".join(files["MOUNTS.txt"].decode().split())
    assert "155 x 92" in notes and "reaches 148 mm forward" in notes
    assert "4 x M2 x 10 (M2 x 12 with the diffuser)" in notes


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


@pytest.mark.parametrize("ring", [False, True])
@pytest.mark.parametrize("reach", [148.0, 100.0])
def test_camera_arm_screws_go_in_from_the_front(reach, ring):
    """M4 x 7 with a 7 mm head: a through hole, nothing in front of the head (it goes in and the
    screwdriver reaches it straight on), a full seat under it, and enough left for the T-nut."""
    from manifold3d import Manifold

    from sortering_pnp.printables.mounts import screw_seat

    p = CameraArm(reach=reach, ring=ring)
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
    assert p.reach == 148.0
    v, _ = _to_mesh(_camera_arm_installed(p))
    assert v[:, 2].min() == pytest.approx(-p.bar, abs=0.01)  # the plate on the bar
    v = v[v[:, 1] < -p.reach / 2]  # out over the bed: the bosses are the lowest
    assert v[:, 2].min() == pytest.approx(-p.head_h - p.boss_h, abs=0.01)
    assert lens_position(p) == (0.0, -p.reach, -p.head_h - p.boss_h)
    low = v[v[:, 2] < v[:, 2].min() + 0.01]
    assert low[:, 0].mean() == pytest.approx(0.0, abs=0.05)
    m = trimesh.Trimesh(*build_camera_arm(p))
    assert m.is_watertight and m.body_count == 1
    assert m.bounds[1][2] == pytest.approx(p.bar, abs=0.01)  # lies flat, as high as the plate
    assert b"straight" in dict(mount_files())["MOUNTS.txt"]


def test_camera_arm_is_slim():
    """A tapered T-section beam: less than a solid 16 x 20 mm bar alone (the old arm, that bar
    with a block at its end, was 71 cm3 at 135 mm)."""
    p = CameraArm()
    body = _camera_arm_installed(p)
    assert body.volume() < 16 * 20 * p.reach * 0.7
    # half-way out, the beam is narrower than the bar's height and shallower than at the bar
    from sortering_pnp.printables.gridfinity import _box

    x0, y0, z0, x1, _, z1 = (
        body ^ _box(-50, 50, -p.reach / 2 - 0.5, -p.reach / 2, -50, 50)
    ).bounding_box()
    assert x1 - x0 < 16 and z1 - z0 < p.bar - 2 and z0 > -p.bar


# ---------------------------------------------------------------------- ring light
ALL_ARMS = [CameraArm(), CameraArm(ring=True), CameraArm(drop=60), CameraArm(ring=True, drop=60)]


@pytest.mark.parametrize("ring", [False, True])
def test_both_arms_print_as_one_piece_on_an_ender3(ring):
    from sortering_pnp.printables.mounts import build_ring_parts

    p = CameraArm(ring=ring)
    meshes = {"arm": build_camera_arm(p)}
    if ring:
        meshes |= build_ring_parts(p)
    for name, mesh in meshes.items():
        m = trimesh.Trimesh(*mesh)
        assert m.is_watertight and m.body_count == 1, name
        assert m.bounds[0] == pytest.approx([0, 0, 0], abs=1e-6), name
        assert max(m.bounds[1][:2]) <= 220 and m.bounds[1][2] <= 25, name  # Ender 3 bed, flat


@pytest.mark.parametrize("p", ALL_ARMS, ids=["plain", "ring", "post", "ring+post"])
@pytest.mark.parametrize("lens_shorter", [0.0, 2.0])
@pytest.mark.parametrize("fov", [75.0, 120.0])
def test_nothing_in_the_cameras_view(p, fov, lens_shorter):
    """The view cone (Camera Module 3, diagonal; 120 deg: the Wide) from the lens' front down
    to the bed misses the arm, the bracket, the clamp ring and the diffuser, even if the lens
    is 2 mm shorter than assumed."""
    from dataclasses import replace

    from sortering_pnp.printables.mounts import ring_parts_installed, view_cone

    q = replace(p, lens_depth=p.lens_depth - lens_shorter)
    cone = view_cone(q, fov, depth=600.0)
    assert cone.bounding_box()[2] < -p.bar - 400  # down to the bed and past it
    parts = {"arm": _camera_arm_installed(q)} | ring_parts_installed(replace(q, ring=True))
    for name, part in parts.items():
        assert (part ^ cone).volume() < 1e-3, name


def test_the_lens_looks_down_through_the_bracket_and_the_ring():
    from sortering_pnp.printables.mounts import lens_front, ring_board, ring_parts_installed

    p = CameraArm(ring=True)
    parts = ring_parts_installed(p)
    board = ring_board(p)
    for m in (*parts.values(), board):  # all centred on the lens: over (0, -reach)
        lo, hi = np.array(m.bounding_box()[:3]), np.array(m.bounding_box()[3:])
        assert (lo[0] + hi[0]) / 2 == pytest.approx(0.0, abs=0.01)
    blo, bhi = board.bounding_box()[:3], board.bounding_box()[3:]
    assert (blo[1] + bhi[1]) / 2 + (p.ring_tab_reach - p.ring_od / 2) / 2 == pytest.approx(
        -p.reach, abs=0.01
    )  # the ring's centre (its tab reaches forward)
    # the lens (its unit up to 10.6 mm square) fits through the bracket, its front below it
    from manifold3d import Manifold

    lens = Manifold.cube([10.6, 10.6, 30], True).translate([0, -p.reach, lens_front(p) + 15])
    assert (parts["ring_bracket"] ^ lens).volume() < 1e-3
    assert (board ^ lens).volume() < 1e-3
    assert lens_front(p) < parts["ring_bracket"].bounding_box()[2]


def test_the_ring_sits_in_its_pocket_and_the_clamp_holds_it():
    """The LumenPnP ring has no mounting holes: it lies in a pocket in the bracket's underside
    (40 mm disc and its tab), and the clamp ring (or the diffuser) presses its edge in, clear of
    the LEDs; 4 M2 go through clamp and bracket into the arm's legs."""
    from manifold3d import Manifold

    from sortering_pnp.printables.mounts import ring_board, ring_parts_installed, ring_screws

    p = CameraArm(ring=True)
    parts = ring_parts_installed(p)
    board = ring_board(p)
    assert (parts["ring_bracket"] ^ board).volume() < 1e-3  # it fits the pocket
    # and the pocket is snug: a ring 0.5 mm bigger would not fit
    bigger = Manifold.cylinder(1.0, p.ring_od / 2 + 0.5, p.ring_od / 2 + 0.5, 96)
    zb = parts["ring_bracket"].bounding_box()[2]
    assert (parts["ring_bracket"] ^ bigger.translate([0, -p.reach, zb + 0.2])).volume() > 1.0
    for name in ("ring_clamp", "ring_diffuser"):  # presses the edge, clear of the LEDs
        clamp = parts[name]
        assert (clamp ^ board).volume() > 1.0, name
        # the LEDs and their capacitors: from 10.4 mm (a capacitor near the tab) out
        leds = _ring_annulus_at(p, 10.4, p.ring_parts_r, board.bounding_box()[2] - p.ring_led_h)
        assert (clamp ^ leds).volume() < 1e-3, name
    v, _ = _to_mesh(_camera_arm_installed(p))
    spots = ring_screws(p)
    assert all(_hole_centres(v, p.pilot_d / 2, spots, (0, 1)))  # a pilot in each leg
    for name in ("ring_bracket", "ring_clamp", "ring_diffuser"):
        pv, _ = _to_mesh(parts[name])
        assert all(_hole_centres(pv, p.ring_screw_d / 2, spots, (0, 1))), name
    assert np.hypot(*np.subtract(spots[0], (0, -p.reach))) > p.ring_od / 2 + p.ring_fit + 2


def _ring_annulus_at(p: CameraArm, r0: float, r1: float, z0: float):
    from manifold3d import Manifold

    return Manifold.cylinder(p.ring_led_h, r1, r1, 96).translate([0, -p.reach, z0]) - (
        Manifold.cylinder(p.ring_led_h + 2, max(r0, 0.01), max(r0, 0.01), 96).translate(
            [0, -p.reach, z0 - 1]
        )
    )
