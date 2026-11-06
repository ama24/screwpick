"""Holders on the printer's top bar (Ender 3: a 2020 aluminium profile), fixed with M5 screws
and T-nuts:

- screen holder: an open frame that holds the 7" display (Waveshare 7inch HDMI LCD (H): case
  191 x 115 mm, M2 holes 155 x 92 mm, measured on the screen) by its four holes, leaning back
  so it faces the user. The frame is sized to the holes, not to the case: the display overhangs it. The Raspberry Pi 4 is screwed to the display's back, off centre; it sits
  free inside the frame. A leg at each end stands on the bar (1 screw each in the top slot).
- camera arm: a plate on the bar's front face (2 screws in the front slot, at the bar's centre),
  an arm reaching forward over the bed and a post down to the Camera Module 3 (M2 holes 21 x
  12.5 mm, as on the Camera Module 2), lens straight down at the bed's centre when the bed is
  all the way out (where a print ends: the photo position).

Every size is a parameter: measure your printer and set them (prints/mounts/README.md).
Both are built where they sit on the printer: x along the bar, y towards the back (the bar's
front face at y = 0), z up (the bar's top at z = 0); then turned to print flat, no supports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from manifold3d import Manifold

from .gridfinity import _box, _frustum, _to_mesh, _union
from .meshio import Mesh

SEG = 24


def _cyl_z(r: float, z0: float, z1: float, x: float, y: float) -> Manifold:
    return Manifold.cylinder(z1 - z0, r, r, SEG).translate([x, y, z0])


def _cyl_y(r: float, y0: float, y1: float, x: float, z: float) -> Manifold:
    """A cylinder along y from y0 to y1 at (x, z)."""
    return Manifold.cylinder(y1 - y0, r, r, SEG).rotate([-90, 0, 0]).translate([x, y0, z])


def _to_bed(body: Manifold) -> Mesh:
    lo = np.array(body.bounding_box()[:3])
    return _to_mesh(body.translate([-lo[0], -lo[1], -lo[2]]))


# ---------------------------------------------------------------------- screen holder
@dataclass
class ScreenHolder:
    """An open frame behind the display, just big enough for its four M2 holes, so the Raspberry
    Pi, screwed to the back of the display, sits free inside the frame wherever it is (it is not
    centred on the 7" (H)), and two legs at the ends down to the bar. The display (`screen_w` x
    `screen_h`) overhangs the frame all round."""

    screen_w: float = 191.0  # display case outline
    screen_h: float = 115.0
    screen_r: float = 5.0  # case corner radius
    hole_dx: float = 155.0  # the screen's M2 holes, centre to centre (measured)
    hole_dy: float = 92.0
    hole_d: float = 2.4  # M2 clearance
    head_d: float = 5.6  # room for the M2 screw head (4.8 mm) and the screwdriver
    pad_wall: float = 2.5  # the round pad around each screw: head room + this all round
    tilt_deg: float = 20.0  # the screen leans back this much: it faces the user, a little up
    thickness: float = 3.0
    edge: float = 6.5  # the frame's outer edge this far outside the hole centres
    border: float = 10.0  # width of the frame's band (holes inside it); the rest is open for the Pi
    rim: float = 8.0  # stiffening wall along the outer edge, backwards
    rim_t: float = 2.4
    open_depth: float = 45.0  # free room behind the display inside the frame: Pi + cables
    lift: float = 10.0  # the display's bottom edge at least this far above the bar's top
    leg_reach: float = 34.0  # how far up the frame the legs hold it
    foot_depth: float = 20.0  # the bar's top face (2020)
    foot_out: float = 12.0  # each foot reaches this far beyond the display's side
    foot_t: float = 4.0
    foot_hole_d: float = 5.4  # one M5 in the top slot in each foot, reachable from above

    @property
    def bottom_z(self) -> float:
        """Height of the display's bottom edge above the bar: `lift`, or more when the screen
        leans back so far that the Pi's room would reach down to the bar."""
        s, c = math.sin(math.radians(self.tilt_deg)), math.cos(math.radians(self.tilt_deg))
        return max(self.lift, self.open_depth * s - (self.frame_y0 + self.border + 0.5) * c + 3.0)

    @property
    def frame_w(self) -> float:
        return self.hole_dx + 2 * self.edge

    @property
    def frame_h(self) -> float:
        return self.hole_dy + 2 * self.edge

    @property
    def frame_y0(self) -> float:
        """The frame's bottom edge, up from the display's bottom edge (both centred)."""
        return (self.screen_h - self.frame_h) / 2

    @property
    def foot_hole_dx(self) -> float:
        """Distance between the two M5 screws (one in each foot)."""
        return self.frame_w + self.foot_out


def build_screen_holder(p: ScreenHolder | None = None) -> Mesh:
    """Print orientation: the face the display screws to lies on the bed; the rim and the legs
    rise from it."""
    p = p or ScreenHolder()
    local = (
        _screen_holder_installed(p).translate([0, 0, -p.bottom_z]).rotate([p.tilt_deg - 90, 0, 0])
    )
    return _to_bed(local.mirror([0, 0, 1]))


def _to_installed(m: Manifold, p: ScreenHolder) -> Manifold:
    """From the display's frame (x across, y up the display from its bottom edge, z towards
    the display: z = 0 is its back, the holder lies at z < 0) to where it sits on the bar."""
    return m.rotate([90 - p.tilt_deg, 0, 0]).translate([0, 0, p.bottom_z])


def screen_open_space(p: ScreenHolder | None = None) -> Manifold:
    """Installed: the room behind the display inside the frame, kept free for the Pi."""
    p = p or ScreenHolder()
    b, y0 = p.border + 0.5, p.frame_y0
    room = _box(
        -p.frame_w / 2 + b, p.frame_w / 2 - b, y0 + b, y0 + p.frame_h - b, -p.open_depth, -0.01
    )
    pads = [  # the screw pads reach in from the frame: a little room around each stays taken
        _cyl_z(_pad_r(p) + 0.5, -p.open_depth - 1, 1.0, x, y) for x, y in _screw_spots(p)
    ]
    return _to_installed(room - _union(pads), p)


def _pad_r(p: ScreenHolder) -> float:
    return p.head_d / 2 + p.pad_wall


def _screw_spots(p: ScreenHolder) -> list[tuple[float, float]]:
    """The screen's M2 holes, display frame (x across from the middle, y up from the bottom)."""
    h = p.screen_h
    return [
        (sx * p.hole_dx / 2, y)
        for sx in (-1, 1)
        for y in ((h - p.hole_dy) / 2, (h + p.hole_dy) / 2)
    ]


def _screen_holder_installed(p: ScreenHolder) -> Manifold:
    t, w, h, y0 = p.thickness, p.frame_w, p.frame_h, p.frame_y0
    yc = y0 + h / 2  # the frame's centre: the display's centre, the holes are centred on it
    outline = _frustum([(w, h, p.screen_r, -t), (w, h, p.screen_r, 0.0)], 0.0, yc)
    inner = _frustum(
        [
            (w - 2 * p.border, h - 2 * p.border, 0.5, -t - 1),
            (w - 2 * p.border, h - 2 * p.border, 0.5, 1),
        ],
        0.0,
        yc,
    )
    rim_outline = _frustum([(w, h, p.screen_r, -t - p.rim), (w, h, p.screen_r, -t + 0.01)], 0.0, yc)
    rim_inner = _frustum(
        [
            (w - 2 * p.rim_t, h - 2 * p.rim_t, max(p.screen_r - p.rim_t, 0.5), -t - p.rim - 1),
            (w - 2 * p.rim_t, h - 2 * p.rim_t, max(p.screen_r - p.rim_t, 0.5), 0.0),
        ],
        0.0,
        yc,
    )
    # a round pad at each screw, joined to the frame's band: the holes lie a little inside it
    pads = [_cyl_z(_pad_r(p), -t, 0.0, x, y) for x, y in _screw_spots(p)]
    frame = (outline - inner) + (rim_outline - rim_inner) + _union(pads)
    installed = _to_installed(frame, p)
    # a leg at each end: from the frame's bottom corner down to a foot on the bar's top
    legs = []
    for sx in (-1, 1):
        x0, x1 = sorted((sx * w / 2, sx * (w / 2 - p.border)))
        block = _to_installed(_box(x0, x1, y0, y0 + p.leg_reach, -t - p.rim, 0.0), p)
        root = _box(x0, x1, 0.0, p.foot_depth, 0.0, p.foot_t)
        legs.append(Manifold.batch_hull([block, root]))
        fx0, fx1 = sorted((sx * (w / 2 - p.border), sx * (w / 2 + p.foot_out)))
        legs.append(_box(fx0, fx1, 0.0, p.foot_depth, 0.0, p.foot_t))
    holes_m5 = [
        _cyl_z(p.foot_hole_d / 2, -1, p.foot_t + 1, sx * p.foot_hole_dx / 2, p.foot_depth / 2)
        for sx in (-1, 1)
    ]
    # M2 holes into the display, through the frame (and the legs at the bottom corners); from
    # the back a counterbore leaves `thickness` of material, so M2 x 6-8 screws reach
    m2 = []
    for x, y in _screw_spots(p):
        m2.append(_cyl_z(p.hole_d / 2, -80.0, 1.0, x, y))
        m2.append(_cyl_z(p.head_d / 2, -80.0, -t, x, y))
    return (installed + _union(legs)) - _union(holes_m5) - _to_installed(_union(m2), p)


# ---------------------------------------------------------------------- camera arm
@dataclass
class CameraArm:
    reach: float = 135.0  # bar front face -> lens, horizontally (measure with the bed out)
    drop: float = 60.0  # the camera plate this far below the bar's underside (lens ~300 mm up)
    bar: float = 20.0  # 2020 profile: front face height
    plate_w: float = 50.0
    plate_t: float = 6.0
    screw_dx: float = 30.0  # 2 x M5 in the front slot, at the bar's centre, this far apart
    screw_d: float = 5.4
    arm_w: float = 16.0
    cam_w: float = 25.0  # Camera Module 3 board (same as Camera Module 2)
    cam_h: float = 23.86
    cam_hole_dx: float = 21.0
    cam_hole_rows: tuple[float, float] = (2.0, 14.5)  # from the cable edge
    lens_from_cable_edge: float = 14.5  # the lens is about on the second row
    cable_gap: float = 4.0  # room for the flat cable to bend up along the post
    boss_h: float = 4.0  # room for the parts on the board's back
    boss_d: float = 5.0
    pilot_d: float = 1.8  # M2 screws cut their own thread


def build_camera_arm(p: CameraArm | None = None) -> Mesh:
    """Print orientation: upside down (the top of the arm and of the mounting plate on the
    bed); the post and the camera plate grow upwards, no supports."""
    return _to_bed(_camera_arm_installed(p or CameraArm()).rotate([180, 0, 0]))


def _camera_arm_installed(p: CameraArm) -> Manifold:
    b = p.bar
    mount = _box(-p.plate_w / 2, p.plate_w / 2, -p.plate_t, 0.0, -b, 0.0) - _union(
        [_cyl_y(p.screw_d / 2, -p.plate_t - 1, 1, sx * p.screw_dx / 2, -b / 2) for sx in (-1, 1)]
    )
    edge_y = -p.reach + p.lens_from_cable_edge  # the board's cable edge (towards the bar)
    post_y0 = edge_y + p.cable_gap
    post_y1 = post_y0 + p.arm_w
    arm = _box(-p.arm_w / 2, p.arm_w / 2, post_y0, -p.plate_t + 0.01, -b, 0.0)
    gussets = [
        Manifold.hull_points(
            np.array(
                [(sx * p.arm_w / 2, -p.plate_t + 0.01, z) for z in (-b, 0.0)]
                + [(sx * p.plate_w * 0.4, -p.plate_t + 0.01, z) for z in (-b, 0.0)]
                + [(sx * p.arm_w / 2, -p.plate_t - 22.0, z) for z in (-b, 0.0)]
            )
        )
        for sx in (-1, 1)
    ]
    z_cam = -b - p.drop  # underside of the camera plate (the bosses hang below it)
    cam_plate_w = p.cam_w + 8.0
    cam_y0, cam_y1 = edge_y - p.cam_h - 3.0, post_y1
    rise = max((cam_plate_w - p.arm_w) / 2, (post_y0 - cam_y0)) + 1.0  # flare at <= 45 deg
    post = _box(-p.arm_w / 2, p.arm_w / 2, post_y0, post_y1, z_cam + 3.0, -b + 0.01)
    flare = Manifold.hull_points(
        np.array(
            [
                (x, y, z_cam + 3.0)
                for x in (-cam_plate_w / 2, cam_plate_w / 2)
                for y in (cam_y0, cam_y1)
            ]
            + [
                (x, y, min(z_cam + 3.0 + rise, -b))
                for x in (-p.arm_w / 2, p.arm_w / 2)
                for y in (post_y0, post_y1)
            ]
        )
    )
    cam_plate = _box(-cam_plate_w / 2, cam_plate_w / 2, cam_y0, cam_y1, z_cam, z_cam + 3.0)
    bosses, pilots = [], []
    for sx in (-1, 1):
        for row in p.cam_hole_rows:
            x, y = sx * p.cam_hole_dx / 2, edge_y - row
            bosses.append(_cyl_z(p.boss_d / 2, z_cam - p.boss_h, z_cam + 0.01, x, y))
            pilots.append(_cyl_z(p.pilot_d / 2, z_cam - p.boss_h - 1, z_cam + 2.0, x, y))
    return _union([mount, arm, *gussets, post, flare, cam_plate, *bosses]) - _union(pilots)


def lens_position(p: CameraArm | None = None) -> tuple[float, float, float]:
    """Installed: (x, y, z) of the lens centre, at the board's front (bar top = 0)."""
    p = p or CameraArm()
    return 0.0, -p.reach, -p.bar - p.drop - p.boss_h


def drop_for(
    bar_underside_above_plate: float, lens_height: float = 300.0, p: CameraArm | None = None
) -> float:
    """The `drop` that puts the lens `lens_height` above the plate (the scale the model was
    trained at: about 300 mm), given the measured height of the bar's underside."""
    p = p or CameraArm()
    drop = bar_underside_above_plate - lens_height - p.boss_h
    if drop < 0:
        raise ValueError(
            f"the bar is only {bar_underside_above_plate:g} mm above the plate: the lens cannot be "
            f"{lens_height:g} mm up (set scenegen.nominal_camera.height_mm to the real height)"
        )
    return round(drop, 1)


MOUNT_NOTES = """screwpick holders for the printer's top bar
============================================

Both fix to the top bar (Ender 3: a 2020 aluminium profile) with M5 screws and T-nuts (drop-in
T-nuts go in without taking the printer apart). Print in PETG or PLA, 0.2 mm layers, 4 walls,
30 % infill, lying as the files are; no supports.

screen_holder.stl: an open frame for the Waveshare 7inch HDMI LCD (H) (case 191 x 115 mm,
  four M2 holes 155 x 92 mm): M2 x 6-8 screws (heads up to 5.6 mm) through the frame into the
  case. The frame ({fw:g} x {fh:g} mm) is just big enough for the holes, so the display
  overhangs it; it is a {border:g} mm band: the Raspberry Pi stays screwed to the display's back
  where it is (off centre) and sits free in the {ow:g} x {oh:g} mm opening, with
  {depth:g} mm of room behind the display for it and its cables. A leg at each end stands on
  the bar, the display's bottom {lift:g} mm above it: 2 x M5 x 8 into the top slot, one in each
  foot, {dx:g} mm apart. The screen leans back {tilt:g} degrees, so it faces you. Put it on one
  side of the bar; the camera is in the middle.

camera_arm.stl: the plate goes on the bar's front face, at the bar's centre: 2 x M5 x 10 into
  the front slot. The arm reaches {reach:g} mm forward and the post goes {drop:g} mm down: the
  lens is then over the bed's centre with the bed all the way out (where a print ends, the
  photo position), {lens:g} mm below the bar's top. The Camera Module 3 screws on with
  4 x M2 x 6 (they cut their own thread), lens down, the flat cable towards the post.

Your printer differs? Measure and make your own (web page: Print bins; or
screwpick print mounts --help):
  reach:      with the bed all the way out, from the front face of the top bar straight
              forward to the centre of the plate (where the parts lie).
  bar height: from the underside of the top bar down to the plate. The camera is placed
              300 mm above the plate, the height the model was trained for (about 11.7 px/mm).
  tilt:       how far the screen leans back.
"""


def mount_files(
    screen: ScreenHolder | None = None, arm: CameraArm | None = None
) -> list[tuple[str, bytes]]:
    from .meshio import stl_bytes

    screen, arm = screen or ScreenHolder(), arm or CameraArm()
    lens = -lens_position(arm)[2]
    notes = MOUNT_NOTES.format(
        tilt=screen.tilt_deg,
        border=screen.border,
        fw=screen.frame_w,
        fh=screen.frame_h,
        ow=screen.frame_w - 2 * screen.border,
        oh=screen.frame_h - 2 * screen.border,
        depth=screen.open_depth,
        lift=round(screen.bottom_z, 1),
        dx=screen.foot_hole_dx,
        reach=arm.reach,
        drop=arm.drop,
        lens=lens,
    )
    return [
        ("screen_holder.stl", stl_bytes(build_screen_holder(screen), "screwpick screen holder")),
        ("camera_arm.stl", stl_bytes(build_camera_arm(arm), "screwpick camera arm")),
        ("MOUNTS.txt", notes.encode()),
    ]
