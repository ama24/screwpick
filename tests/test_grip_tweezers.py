"""Grip point, centre of mass and axis; tweezers turned across the part; the console."""

from __future__ import annotations

import math

import numpy as np
import pytest

from sortering_pnp.grip import Shape, axis_grip, com_from_bearing, jaw_angle, outline_grip
from sortering_pnp.layout import Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D
from sortering_pnp.vision import parts_from_result

M4_SOCKET = Shape(d=4.0, head_r=3.5, head_k=4.0, socket=True)  # DIN912 M4: dk 7, k 4


def test_centre_of_mass_moves_towards_the_head():
    assert com_from_bearing(20, Shape(d=4.0)) == pytest.approx(10.0)  # headless: the middle
    long = com_from_bearing(40, M4_SOCKET)
    short = com_from_bearing(6, M4_SOCKET)
    assert 0 < long < 20  # pulled towards the head
    assert short < 0  # a short screw: the centre of mass lies inside the head


def kp_line(angle_deg: float, head_k: float, length: float, start=(50.0, 50.0)):
    u = np.array([math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))])
    ht = np.array(start)
    return {"head_top": ht, "bearing": ht + u * head_k, "tip": ht + u * (head_k + length)}, u


@pytest.mark.parametrize("angle", [0.0, 30.0, 135.0])
def test_axis_grip_on_the_shank(angle):
    kp, u = kp_line(angle, 4.0, 25.0)
    g = axis_grip(kp, M4_SOCKET)
    assert g.axis_deg == pytest.approx(angle % 180)
    along = float(np.dot(np.array(g.point) - kp["bearing"], u))
    com = float(np.dot(np.array(g.com) - kp["bearing"], u))
    assert along == pytest.approx(max(com, 1.5))  # at the centre of mass, on the shank
    # a short screw: never on the head, never off the tip
    kp, u = kp_line(angle, 4.0, 5.0)
    g = axis_grip(kp, M4_SOCKET)
    along = float(np.dot(np.array(g.point) - kp["bearing"], u))
    assert 1.5 - 1e-6 <= along <= 4.0 + 1e-6


def test_headless_parts_are_taken_in_the_middle():
    kp, _ = kp_line(90.0, 0.0, 30.0)
    g = axis_grip(kp, Shape(d=6.0))
    assert g.point == pytest.approx((50.0, 65.0)) and g.axis_deg == pytest.approx(90.0)


def test_outline_grip_rectangle():
    a = math.radians(30)
    u, v = np.array([math.cos(a), math.sin(a)]), np.array([-math.sin(a), math.cos(a)])
    c = np.array([10.0, 20.0])
    rect = np.array(
        [c - 10 * u - 2 * v, c + 10 * u - 2 * v, c + 10 * u + 2 * v, c - 10 * u + 2 * v]
    )
    for poly in (rect, rect[::-1]):  # either winding
        g = outline_grip(poly)
        assert g.point == pytest.approx((10.0, 20.0)) and g.axis_deg == pytest.approx(30.0)
    assert outline_grip(np.zeros((2, 2))) is None


def test_jaw_angle_closes_across_the_part():
    assert jaw_angle(0.0) == pytest.approx(90.0)  # part along X: jaws close along Y
    assert jaw_angle(120.0) == pytest.approx(30.0)
    assert jaw_angle(10.0, offset_deg=-5.0) == pytest.approx(95.0)


def test_parts_from_result_uses_the_keypoints():
    obj = {
        "id": 1,
        "class": "DIN912_M4",
        "score": 0.9,
        "polygon": [[0, 0], [290, 0], [290, 70], [0, 70]],
        "position_mm": [14.5, 3.5],
        "orientation_deg": 0.0,
        "length": {"length_mm": 25.0},
        "keypoints": {"classical": {"head_top": [0, 35], "bearing": [40, 35], "tip": [290, 35]}},
    }
    parts = parts_from_result(
        {"objects": [obj]},
        lambda px, h: np.c_[np.asarray(px, float) * 0.1, np.full(len(px), h)],
        {"DIN912_M4": 3.5}.get,
        {"DIN912_M4": M4_SOCKET}.get,
    )
    p = parts[0]
    assert p.grip_method == "axis" and p.grip_height_mm == pytest.approx(2.0)
    assert 4.0 + 1.5 <= p.grip_plate[0] < 4.0 + 12.5  # on the shank, towards the head
    assert p.grip_plate[1] == pytest.approx(3.5)
    no_kp = dict(obj, keypoints=None)
    q = parts_from_result(
        {"objects": [no_kp]}, lambda px, h: np.c_[px * 0.1, px[:, :1] * 0], {"DIN912_M4": 3.5}.get
    )[0]
    assert q.grip_method == "outline" and q.grip_plate == pytest.approx((14.5, 3.5))


def make(machine, n: int = 4):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=9)
    world.populate(lay, n=n)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    printer = SimPrinter(machine, world)
    return Runner(machine, printer, vision), world, printer


def _turns(printer, prefix: str) -> list[float]:
    """The jaw angles sent, in order: '; pins turn 12.3' (the servo) or 'MACRO ANGLE=12.3'."""
    out = []
    for ln in printer.log:
        if ln.startswith("; pins turn "):
            out.append(float(ln.split()[-1]))
        elif prefix and ln.startswith(prefix):
            out.append(float(ln.split("ANGLE=")[1]))
    return out


@pytest.mark.parametrize("stepper", [False, True])
def test_tweezers_turn_across_each_part_and_the_console_says_so(machine, stepper):
    machine.picker.mode = "tweezers"
    if stepper:  # tweezers on a stepper: Klipper macros (klipper/sortering_pnp.cfg)
        machine.picker.rotate_macro = "SORTERING_ROTATE"
        machine.picker.rotate_home = "SORTERING_ROT_HOME"
    machine.run.shake.enabled = False
    runner, world, printer = make(machine)
    angles = {id(sp): sp.angle for sp in world.parts}
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done" and not world.parts
    turns = _turns(printer, "SORTERING_ROTATE" if stepper else "")
    assert len(turns) == len(world.dropped)
    for (sp, _), got in zip(world.dropped, turns, strict=True):
        want = (angles[id(sp)] + 90.0) % 180.0
        assert min(abs(got - want), 180 - abs(got - want)) < 0.06
    # the servo turns on the Pi's pin, a stepper through Klipper (and finds its 0 first)
    assert any(ln.startswith("; pins turn") for ln in printer.log) != stepper
    assert ("SORTERING_ROT_HOME" in printer.log) == stepper
    console = runner.snapshot()["console"]
    text = "\n".join(console)
    assert "sorting: sort" in text and "on the plate:" in text and "jaws" in text
    assert "leaving " in text and " in bin " in text and "done:" in text
    assert "taking a photo" in text and "running the model" in text
    assert all(len(line) < 100 for line in console)  # short lines


def test_setup_rotation_test(machine):
    machine.picker.mode = "tweezers"
    runner, world, printer = make(machine, n=0)
    runner.setup_picker("grab")
    runner.setup_picker("release")
    assert printer.log[-2:] == ["; pins grab", "; pins release"]  # on the Pi's pins
    with pytest.raises(Exception, match="0° and 90°"):
        runner.setup_confirm("picker")
    runner.setup_picker_offset(12)
    runner.setup_rotate(90)
    assert printer.jaw_angle == 102.0  # the trim is added
    runner.setup_confirm("picker")
    assert runner.setup.is_done("picker")
