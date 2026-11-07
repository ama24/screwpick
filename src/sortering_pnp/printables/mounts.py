"""Holders on the printer's top bar (Ender 3: a 2020 aluminium profile), fixed with screws
and T-nuts (the screen holder M5, the camera arm M4 x 7):

- screen holder: an open frame that holds the 7" display (Waveshare 7inch HDMI LCD (H): case
  191 x 115 mm, M2 holes 155 x 92 mm, measured on the screen) by its four holes, leaning back
  so it faces the user. The frame is sized to the holes, not to the case: the display overhangs it. The Raspberry Pi 4 is screwed to the display's back, off centre; it sits
  free inside the frame. A leg at each end stands on the bar (1 screw each in the top slot).
- camera arm: a plate on the bar's front face (2 x M4 x 7 from the front into T-nuts in the
  front slot, at the bar's centre; counterbored, the beam's flare leaves room for the
  screwdriver), a slim T-section beam reaching forward over the bed, tapering from the bar's
  height to a small pod with the Camera Module 3 (M2 holes 21 x 12.5 mm, as on the Camera
  Module 2) under it, lens straight down at the bed's centre when the bed is all the way out
  (where a print ends: the photo position). The camera is as high as it can be, so it sees the
  most of the bed; `drop` adds a post down for a lower camera.
- ring light (`CameraArm(ring=True)`): legs beside the camera hold a bracket under it with
  the Opulo LumenPnP top ring light (a 40 mm disc, 20 mm hole, no mounting holes) in a pocket
  in its underside, the lens looking down through both holes; a clamp ring or a diffuser
  under the LEDs presses it in, the same 4 M2 screws through all of them into the legs.

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
FOV_STANDARD = 75.0  # Camera Module 3: diagonal field of view in degrees (66 x 41 deg)
FOV_WIDE = 120.0  # Camera Module 3 Wide (102 x 67 deg)


@dataclass
class CameraArm:
    """A slim T-section beam from the bar's front face to a small pod with the camera under it.
    With `ring`, four legs hang down beside the camera for the ring-light bracket (the Opulo
    LumenPnP top ring light; `ring_*` sizes from its KiCad board, src/PCB/ringLight): camera ->
    bracket -> ring -> clamp ring or diffuser, all four held by the same 4 x M2 screws."""

    reach: float = 148.0  # bar front face -> lens, horizontally (measure with the bed out)
    drop: float = 0.0  # 0: the camera right under the arm (sees the most); > 0: a post down
    ring: bool = False  # legs for the ring-light bracket (camera_arm_ring.stl)
    bar: float = 20.0  # 2020 profile: front face height
    plate_w: float = 60.0
    plate_t: float = 6.0
    plate_r: float = 4.0  # the plate's rounded corners
    screw_dx: float = 40.0  # 2 screws in the front slot, at the bar's centre, this far apart
    screw_d: float = 4.5  # M4 clearance
    head_d: float = 7.6  # counterbore and screwdriver room: M4 head of 7 mm
    screw_len: float = 7.0  # under the head
    screw_beyond: float = 4.5  # past the plate's back: the slot's lip and the T-nut's thread
    beam_w: float = 16.0  # the beam's top flange at the bar; it narrows to `beam_tip_w`
    beam_tip_w: float = 11.0
    flange_t: float = 3.0
    web_t: float = 6.0  # the beam's upright blade under the flange (a T: stiff, light, no supports)
    head_h: float = 12.0  # the beam's depth at its end and the camera pod's (`bar` at the bar)
    chamfer: float = 1.5  # on the beam's lower edges
    flare: float = 18.0  # the beam widens into the plate over this length
    cam_w: float = 25.0  # Camera Module 3 board (same as Camera Module 2)
    cam_h: float = 23.86
    cam_hole_dx: float = 21.0
    cam_hole_rows: tuple[float, float] = (2.0, 14.5)  # from the cable edge
    lens_from_cable_edge: float = 14.5  # the lens is about on the second row
    cable_gap: float = 4.0  # room for the flat cable to bend up along the post
    boss_h: float = 4.0  # room for the parts on the board's back
    boss_d: float = 5.0
    pilot_d: float = 1.8  # M2 screws cut their own thread
    board_t: float = 1.0  # the camera's board
    lens_depth: float = 10.0  # the board's back to the lens' front (the module is 11.5 mm deep)
    lens_front_r: float = 2.5  # the lens' front opening: the field of view starts there
    # ring light: Opulo LumenPnP top ring light (PCB-0002, opulo-inc/lumenpnp src/PCB/ringLight)
    ring_od: float = 40.0  # a 40 mm disc, 1.6 mm FR4, no mounting holes
    ring_id: float = 20.0
    ring_t: float = 1.6
    ring_tab_w: float = 12.0  # the connector's tab: 12 mm wide, out to 30.25 mm from the centre
    ring_tab_reach: float = 30.25
    ring_parts_r: float = 18.6  # the 8 WS2812B and their capacitors lie inside this
    ring_led_h: float = 1.6  # WS2812B 5050: 1.6 mm high
    ring_fit: float = 0.2  # pocket clearance all round
    ring_pocket: float = 1.5  # pocket depth: the 1.6 mm board stands proud, the clamp presses it
    ring_screw_dx: float = 25.0  # the 4 M2 through clamp, bracket and into the legs (r = 24 mm)
    ring_screw_dy: float = 41.0
    ring_screw_d: float = 2.4  # M2 clearance
    leg_r: float = 2.6
    leg_pilot: float = 9.0  # pilot depth in each leg
    bracket_gap: float = 3.0  # the board's front (lens side) to the bracket: its parts, screw heads
    bracket_t: float = 2.4  # the bracket over the ring
    bracket_r: float = 27.0
    lens_hole_d: float = 15.0  # the lens unit goes through (up to 10.6 mm square)
    plug_slot: tuple[float, float, float] = (5.5, 24.6, 31.5)  # vertical JST XH: half width, radii
    clamp_t: float = 1.6
    clamp_lip: float = 1.2  # how far the clamp reaches over the ring's edge
    diffuser_t: float = 1.0  # the light goes through this (white or clear PETG)
    diffuser_air: float = 0.8  # between the LEDs and the diffuser
    diffuser_wall: tuple[float, float] = (9.0, 10.3)  # inner baffle: keeps the glare off the lens


def build_camera_arm(p: CameraArm | None = None) -> Mesh:
    """Print orientation: upside down (the top of the arm and of the mounting plate on the
    bed); the beam's blade, the camera's pod and its bosses (and legs, post) grow upwards."""
    return _to_bed(_camera_arm_installed(p or CameraArm()).rotate([180, 0, 0]))


def screw_seat(p: CameraArm) -> float:
    """Plate left under each screw head, so `screw_len` reaches `screw_beyond` past the back."""
    seat = p.screw_len - p.screw_beyond
    if not 1.5 <= seat <= p.plate_t:
        raise ValueError(
            f"a {p.screw_len:g} mm screw leaves {seat:g} mm under its head: use one "
            f"{p.screw_beyond + 1.5:g} to {p.screw_beyond + p.plate_t:g} mm long"
        )
    return seat


FLAT_DROP = 8.0  # below this no post fits: the camera goes right under the arm


def _rrect(x0: float, x1: float, y0: float, y1: float, r: float, z0: float, z1: float) -> Manifold:
    r = min(r, (x1 - x0) / 2 - 0.01, (y1 - y0) / 2 - 0.01)
    return Manifold.batch_hull(
        [_cyl_z(r, z0, z1, x, y) for x in (x0 + r, x1 - r) for y in (y0 + r, y1 - r)]
    )


def _hull(points: list[tuple[float, float, float]]) -> Manifold:
    return Manifold.hull_points(np.array(points, dtype=float))


def _beam_section(
    y: float, half: float, depth: float, c: float
) -> list[tuple[float, float, float]]:
    """A section across the beam at y: `half` wide each side, from z = 0 down to -depth, the
    lower edges chamfered by c."""
    c = min(c, half - 0.3, depth - 0.3)
    return [
        (sx * x, y, z)
        for sx in (-1, 1)
        for x, z in ((half, 0.0), (half, -depth + c), (half - c, -depth))
    ]


def _cable_edge(p: CameraArm) -> float:
    return -p.reach + p.lens_from_cable_edge


def ring_screws(p: CameraArm) -> list[tuple[float, float]]:
    """Installed (x, y) of the 4 M2 that hold the clamp, bracket and ring to the legs."""
    return [
        (sx * p.ring_screw_dx / 2, -p.reach + sy * p.ring_screw_dy / 2)
        for sx in (-1, 1)
        for sy in (-1, 1)
    ]


def _pod_circles(p: CameraArm) -> list[tuple[float, float, float]]:
    """The pod's underside, as rounded corners (x, y, r): the board and 2 mm round it, and the
    legs' tops with the ring."""
    edge = _cable_edge(p)
    x1, y0, y1, r = p.cam_w / 2 + 2.0, edge - p.cam_h - 2.0, edge + 2.0, 4.0
    circles = [(sx * (x1 - r), y, r) for sx in (-1, 1) for y in (y0 + r, y1 - r)]
    if p.ring:
        circles += [(x, y, p.leg_r) for x, y in ring_screws(p)]
    return circles


def _slab(circles: list[tuple[float, float, float]], z0: float, z1: float) -> list[Manifold]:
    return [_cyl_z(r, z0, z1, x, y) for x, y, r in circles]


def bracket_top(p: CameraArm) -> float:
    """Installed z of the bracket's top (the legs' ends)."""
    return lens_position(p)[2] - p.board_t - p.bracket_gap


def _camera_arm_installed(p: CameraArm) -> Manifold:
    b = p.bar
    seat = screw_seat(p)
    screws = [sx * p.screw_dx / 2 for sx in (-1, 1)]
    gusset_x = screws[1] - p.head_d / 2 - 1.0  # the flare stops short of the screw heads
    pod = _pod_circles(p)
    pod_x = max(abs(x) + r for x, _, r in pod)
    if min(gusset_x - p.beam_w / 2, p.plate_w / 2 - screws[1] - p.head_d / 2) < 2:
        raise ValueError("the screws must lie between the arm and the plate's ends")
    if pod_x > screws[1] - p.head_d / 2 - 0.3:
        raise ValueError("the camera's pod would be in the way of the screwdriver")
    r = p.plate_r
    mount = Manifold.batch_hull(
        [
            _cyl_y(r, -p.plate_t, 0.0, sx * (p.plate_w / 2 - r), z)
            for sx in (-1, 1)
            for z in (-r, -b + r)
        ]
    )
    # through holes, and in front of each a free channel (counterbore, head, screwdriver)
    holes = [_cyl_y(p.screw_d / 2, -p.plate_t - 1, 1, x, -b / 2) for x in screws] + [
        _cyl_y(p.head_d / 2, -p.reach - 60.0, -seat, x, -b / 2) for x in screws
    ]
    y0 = -p.plate_t + 0.01
    pod_y0 = min(y - r for _, y, r in pod)
    pod_y1 = max(y + r for _, y, r in pod)
    y_end = pod_y1 - 3.0  # the beam runs into the pod

    def depth(y: float) -> float:  # the beam tapers from the bar's height to the pod's
        return b + (p.head_h - b) * (y0 - y) / (y0 - y_end)

    c, ft = p.chamfer, p.flange_t
    yf = y0 - p.flare
    beam = [
        _hull(
            _beam_section(y0, p.beam_w / 2, ft, c) + _beam_section(y_end, p.beam_tip_w / 2, ft, c)
        ),
        _hull(
            _beam_section(y0, p.web_t / 2, b, c) + _beam_section(y_end, p.web_t / 2, p.head_h, c)
        ),
        # the flare into the plate: the flange widens to the screws, the blade thickens
        _hull(_beam_section(y0, gusset_x, ft, c) + _beam_section(yf, p.beam_w / 2, ft, c)),
        _hull(
            _beam_section(y0, p.web_t / 2 + 4.0, b, c)
            + _beam_section(yf, p.web_t / 2, depth(yf), c)
        ),
    ]
    z_cam = -p.head_h - p.drop  # the pod's underside (the bosses hang below it)
    top = [(x, y, max(rr - 3.0, 0.5)) for x, y, rr in pod]  # the pod narrows 3 mm to the top
    if p.drop < FLAT_DROP:  # no post: the pod right under the beam's end
        parts = [Manifold.batch_hull(_slab(pod, z_cam, z_cam + 0.01) + _slab(top, -0.01, 0.0))]
    else:  # a post down from the beam's end, flaring out at <= 45 deg into the pod
        px, py0 = p.web_t / 2 + 4.0, pod_y1 - 14.0
        rise = max(pod_x - px, py0 - pod_y0) + 1.0
        z_top = min(z_cam + rise, -p.head_h)
        post = _rrect(-px, px, py0, pod_y1, 3.0, z_top - 0.01, 0.0)
        flare = Manifold.batch_hull(
            [
                *_slab(pod, z_cam, z_cam + 0.01),
                _rrect(-px, px, py0, pod_y1, 3.0, z_top - 0.01, z_top),
            ]
        )
        parts = [post, flare]
    edge = _cable_edge(p)
    parts += _camera_bosses(p, edge, z_cam)
    cuts = _camera_pilots(p, edge, z_cam)
    if p.ring:  # legs down beside the camera to the bracket, a pilot hole up each
        zb = bracket_top(p)
        parts += [_cyl_z(p.leg_r, zb, z_cam + 0.01, x, y) for x, y in ring_screws(p)]
        cuts += [_cyl_z(p.pilot_d / 2, zb - 1.0, zb + p.leg_pilot, x, y) for x, y in ring_screws(p)]
    return _union([mount, *beam, *parts]) - _union([*cuts, *holes])


def _camera_spots(p: CameraArm, edge_y: float) -> list[tuple[float, float]]:
    return [(sx * p.cam_hole_dx / 2, edge_y - row) for sx in (-1, 1) for row in p.cam_hole_rows]


def _camera_bosses(p: CameraArm, edge_y: float, z_cam: float) -> list[Manifold]:
    return [
        _cyl_z(p.boss_d / 2, z_cam - p.boss_h, z_cam + 0.01, x, y)
        for x, y in _camera_spots(p, edge_y)
    ]


def _camera_pilots(p: CameraArm, edge_y: float, z_cam: float) -> list[Manifold]:
    return [
        _cyl_z(p.pilot_d / 2, z_cam - p.boss_h - 1, z_cam + 6.0, x, y)
        for x, y in _camera_spots(p, edge_y)
    ]


def lens_position(p: CameraArm | None = None) -> tuple[float, float, float]:
    """Installed: (x, y, z) of the lens centre, at the board's back (bar top = 0)."""
    p = p or CameraArm()
    return 0.0, -p.reach, -p.head_h - p.drop - p.boss_h


def lens_front(p: CameraArm | None = None) -> float:
    """Installed z of the lens' front, where its field of view starts."""
    p = p or CameraArm()
    return lens_position(p)[2] - p.lens_depth


def view_cone(p: CameraArm, fov_deg: float = FOV_STANDARD, depth: float = 400.0) -> Manifold:
    """Installed: what the camera sees, a cone from the lens' front down `depth` mm (the
    diagonal field of view: it holds the picture's whole rectangle)."""
    r_far = p.lens_front_r + depth * math.tan(math.radians(fov_deg / 2))
    cone = Manifold.cylinder(depth, r_far, p.lens_front_r, 96)
    return cone.translate([0.0, -p.reach, lens_front(p) - depth])


# ---------------------------------------------------------------------- ring light
RING_SEG = 96


def _ring_local_screws(p: CameraArm) -> list[tuple[float, float]]:
    return [(x, y + p.reach) for x, y in ring_screws(p)]


def _tab_notch(p: CameraArm, z0: float, z1: float) -> Manifold:
    """Room for the connector's tab (forward, -y): the clamp and diffuser leave it open."""
    w = p.ring_tab_w / 2 + 1.0
    return _box(-w, w, -p.bracket_r - 10.0, -17.0, z0, z1)


def _bracket_local(p: CameraArm) -> Manifold:
    """Centred on the lens, z = 0 at its underside (the ring's pocket opens there)."""
    h = p.bracket_t + p.ring_pocket
    tab_w, tab_r, fit = p.ring_tab_w / 2, p.ring_tab_reach, p.ring_fit
    body = _union(
        [
            Manifold.cylinder(h, p.bracket_r, p.bracket_r, RING_SEG),
            _rrect(-tab_w - fit - 2.4, tab_w + fit + 2.4, -tab_r - fit - 2.4, 0.0, 2.4, 0.0, h),
        ]
    )
    sw, s0, s1 = p.plug_slot
    cuts = [
        Manifold.cylinder(
            p.ring_pocket + 1, p.ring_od / 2 + fit, p.ring_od / 2 + fit, RING_SEG
        ).translate([0, 0, -1]),
        _box(-tab_w - fit, tab_w + fit, -tab_r - fit, -p.ring_od / 2 + 1.0, -1.0, p.ring_pocket),
        Manifold.cylinder(h + 2, p.lens_hole_d / 2, p.lens_hole_d / 2, RING_SEG).translate(
            [0, 0, -1]
        ),
        _box(-sw, sw, -s1, -s0, -1.0, h + 1.0),  # the vertical plug goes up through here
    ] + [_cyl_z(p.ring_screw_d / 2, -1.0, h + 1.0, x, y) for x, y in _ring_local_screws(p)]
    return body - _union(cuts)


def _ring_annulus(r0: float, r1: float, z0: float, z1: float) -> Manifold:
    return Manifold.cylinder(z1 - z0, r1, r1, RING_SEG).translate([0, 0, z0]) - Manifold.cylinder(
        z1 - z0 + 2, r0, r0, RING_SEG
    ).translate([0, 0, z0 - 1])


def _clamp_local(p: CameraArm, diffuser: bool) -> Manifold:
    """Under the bracket (z = 0 its underside): a flat ring over the ring's edge, or the
    diffuser, a shallow cup under the LEDs open in the middle for the lens."""
    r_in = p.ring_od / 2 - p.clamp_lip
    if r_in < p.ring_parts_r:
        raise ValueError("the clamp ring would press on the LEDs")
    holes = [_cyl_z(p.ring_screw_d / 2, -10.0, 1.0, x, y) for x, y in _ring_local_screws(p)]
    if not diffuser:
        ring = _ring_annulus(r_in, p.bracket_r, -p.clamp_t, 0.0)
        return ring - _union([_tab_notch(p, -5.0, 1.0), *holes])
    w0, w1 = p.diffuser_wall
    air = p.ring_led_h + p.diffuser_air
    cup = _ring_annulus(w0, p.bracket_r, -air - p.diffuser_t, 0.0)
    recess = _ring_annulus(w1, r_in, -air, 1.0)
    baffle_top = Manifold.cylinder(1.3, w1, w1, RING_SEG).translate([0, 0, -0.3])
    return cup - _union([recess, baffle_top, _tab_notch(p, -10.0, 1.0), *holes])


def _bracket_bottom(p: CameraArm) -> float:
    return bracket_top(p) - p.bracket_t - p.ring_pocket


def ring_parts_installed(p: CameraArm | None = None) -> dict[str, Manifold]:
    """The bracket, clamp ring and diffuser where they sit under the camera."""
    p = p or CameraArm(ring=True)
    at = [0.0, -p.reach, _bracket_bottom(p)]
    return {
        "ring_bracket": _bracket_local(p).translate(at),
        "ring_clamp": _clamp_local(p, False).translate(at),
        "ring_diffuser": _clamp_local(p, True).translate(at),
    }


def ring_board(p: CameraArm | None = None) -> Manifold:
    """Installed: the ring light's board (a model for the checks), pressed into its pocket."""
    p = p or CameraArm(ring=True)
    t, w = p.ring_t, p.ring_tab_w / 2
    disc = _ring_annulus(p.ring_id / 2, p.ring_od / 2, 0.0, t) + _box(
        -w, w, -p.ring_tab_reach, -p.ring_od / 2 + 1.0, 0.0, t
    )
    return disc.translate([0.0, -p.reach, _bracket_bottom(p) + p.ring_pocket - t])


def build_ring_parts(p: CameraArm | None = None) -> dict[str, Mesh]:
    """Print orientation: the bracket with its pocket up, the clamp and the diffuser flat
    (the diffuser's window on the bed)."""
    parts = ring_parts_installed(p)
    return {
        "ring_bracket": _to_bed(parts["ring_bracket"].rotate([180, 0, 0])),
        "ring_clamp": _to_bed(parts["ring_clamp"]),
        "ring_diffuser": _to_bed(parts["ring_diffuser"]),
    }


def drop_for(
    bar_underside_above_plate: float, lens_height: float = 300.0, p: CameraArm | None = None
) -> float:
    """The `drop` that puts the lens `lens_height` above the plate, given the measured height
    of the bar's underside. (Without it the camera sits right under the arm, as high as it goes;
    the calibration measures the real height either way.)"""
    p = p or CameraArm()
    drop = bar_underside_above_plate + p.bar - p.head_h - p.boss_h - lens_height
    if drop < 0:
        raise ValueError(
            f"the bar is only {bar_underside_above_plate:g} mm above the plate: the lens cannot be "
            f"{lens_height:g} mm up (set scenegen.nominal_camera.height_mm to the real height)"
        )
    return round(drop, 1)


MOUNT_NOTES = """sortering-pnp holders for the printer's top bar
============================================

Both fix to the top bar (Ender 3: a 2020 aluminium profile) with screws and T-nuts (drop-in
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

camera_arm.stl: the plate goes on the bar's front face, at the bar's centre: 2 x M{screw:g} x
  {screw_len:g} (heads up to {head:g} mm) from the front into T-nuts in the front slot,
  {screw_dx:g} mm apart; the holes are counterbored and open to the front for the screwdriver.
  A slim T-section beam ({bar:g} mm deep at the bar, {head_h:g} mm at its end) reaches
  {reach:g} mm forward: the lens is then over the bed's centre with the bed all the way out
  (where a print ends, the photo position), {lens:g} mm below the bar's top{post}. The
  Camera Module 3 screws on under its end with 4 x M2 x 6 (they cut their own thread), lens
  down, the flat cable towards the bar.

camera_arm_ring.stl, ring_bracket.stl, ring_clamp.stl, ring_diffuser.stl: the same arm with a
  ring light round the lens, the Opulo LumenPnP top ring light (a 40 mm disc with a 20 mm hole,
  8 WS2812B LEDs, 1.6 mm thick, a 12 mm tab with a 3-pin JST XH; no mounting holes). From the
  top down: the arm, the camera (screwed on as above), the bracket (lens through its
  {lens_hole:g} mm hole), the ring in the bracket's underside (LEDs down, the tab forward, away
  from the bar: a vertical plug goes up through the slot in front of the camera), and under it
  the clamp ring or the diffuser, all held by 4 x M2 x {clamp_screw:g} (M2 x {diffuser_screw:g}
  with the diffuser) through them and the bracket into the arm's legs ({legs_x:g} x {legs_y:g} mm
  apart); the clamp presses the ring's edge into its pocket. The diffuser is optional: print
  it in white or clear PETG, its {dif_t:g} mm window lies under the LEDs, open in the middle
  so the camera sees past it. Nothing is in the camera's view (Camera Module 3, and the
  Wide one).

Your printer differs? Measure and make your own (web page: Print bins; or
sortering_pnp print mounts --help):
  reach:      with the bed all the way out, from the front face of the top bar straight
              forward to the centre of the plate (where the parts lie).
  bar height: only if you want the camera lower: from the underside of the top bar down
              to the plate; a post then puts the lens 300 mm above the plate.
  tilt:       how far the screen leans back.
"""


def mount_files(
    screen: ScreenHolder | None = None, arm: CameraArm | None = None
) -> list[tuple[str, bytes]]:
    """The screen holder, both camera arms (plain and for the ring light), the ring light's
    parts and the notes."""
    from dataclasses import replace

    from .meshio import stl_bytes

    screen, arm = screen or ScreenHolder(), arm or CameraArm()
    plain, ringed = replace(arm, ring=False), replace(arm, ring=True)
    lens = -lens_position(arm)[2]
    bracket = ringed.bracket_t + ringed.ring_pocket
    cup = ringed.ring_led_h + ringed.diffuser_air + ringed.diffuser_t
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
        post=(
            f" (on a post {arm.drop:g} mm down)"
            if arm.drop >= FLAT_DROP
            else " (straight: as high as it goes, it sees the most)"
        ),
        lens=lens,
        bar=arm.bar,
        head_h=arm.head_h,
        screw=round(arm.screw_d - 0.5),
        screw_len=arm.screw_len,
        head=round(arm.head_d - 0.6, 1),
        screw_dx=arm.screw_dx,
        lens_hole=ringed.lens_hole_d,
        clamp_screw=_m2_length(ringed.clamp_t + bracket, ringed),
        diffuser_screw=_m2_length(cup + bracket, ringed),
        legs_x=ringed.ring_screw_dx,
        legs_y=ringed.ring_screw_dy,
        dif_t=ringed.diffuser_t,
    )
    rings = build_ring_parts(ringed)
    return [
        (
            "screen_holder.stl",
            stl_bytes(build_screen_holder(screen), "sortering-pnp screen holder"),
        ),
        ("camera_arm.stl", stl_bytes(build_camera_arm(plain), "sortering-pnp camera arm")),
        (
            "camera_arm_ring.stl",
            stl_bytes(build_camera_arm(ringed), "sortering-pnp camera arm, ring light"),
        ),
        ("ring_bracket.stl", stl_bytes(rings["ring_bracket"], "sortering-pnp ring bracket")),
        ("ring_clamp.stl", stl_bytes(rings["ring_clamp"], "sortering-pnp ring clamp")),
        ("ring_diffuser.stl", stl_bytes(rings["ring_diffuser"], "sortering-pnp ring diffuser")),
        ("MOUNTS.txt", notes.encode()),
    ]


def _m2_length(through: float, p: CameraArm) -> int:
    """A stock M2 length that goes through `through` mm of parts and 4 mm or more into a leg
    (but not past its pilot)."""
    for n in (6, 8, 10, 12, 14, 16, 20):
        if through + 4.0 <= n <= through + p.leg_pilot - 0.5:
            return n
    raise ValueError(f"no stock M2 screw fits {through:g} mm of parts and the legs")
