"""Automatic mode (standby, look, sort, wait), homing over the plate, largest group first, tip height."""

from __future__ import annotations

import time

import numpy as np

from screwpick.layout import Layout
from screwpick.parts import Part
from screwpick.printer.sim import SimPart, SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner, same_parts
from screwpick.strategies import Planner
from screwpick.transform import Transform2D


def make(machine, n: int = 6):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=11)
    world.populate(lay, n=n)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    printer = SimPrinter(machine, world)
    return Runner(machine, printer, vision), world, printer, lay


def until(cond, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_automatic_mode_sorts_waits_and_sorts_new_parts(machine):
    machine.standby.interval_s = 0.2
    machine.standby.settle_s = 0.05
    machine.run.shake.enabled = False
    runner, world, printer, lay = make(machine)
    assert runner.command("auto", "sort").startswith("automatic")
    assert runner.saved_mode() == (True, "sort")
    assert until(lambda: not world.parts and runner.state.status == "standby")
    assert printer.homed == ""  # motors off while waiting
    first = len(world.dropped)
    x0, y0, x1, y1 = lay.work_rect()
    world.parts.append(
        SimPart("DIN934_M4", (x0 + x1) / 2, (y0 + y1) / 2, 0.0, None, (7.0, 7.0), 3.2)
    )
    assert until(lambda: len(world.dropped) == first + 1 and runner.state.status == "standby")
    # homed again before sorting, with Z over the middle of the working area
    homes = [i for i, line in enumerate(printer.log) if line == "G28"]
    assert len(homes) >= 2
    cx = (x0 + x1) / 2
    assert (
        f"SET_GCODE_VARIABLE MACRO=_SCREWPICK_HOME VARIABLE=z_home_x VALUE={cx:.2f}" in printer.log
    )
    runner.shutdown()  # the program ends: automatic mode is remembered
    assert runner.saved_mode()[0] is True
    again, _, _, _ = make(machine, n=0)
    assert again.saved_mode() == (True, "sort")
    again.command("stop")  # the user switches it off
    assert again.saved_mode()[0] is False


def test_automatic_mode_waits_for_parts_to_lie_still(machine):
    machine.standby.interval_s = 0.1
    machine.standby.settle_s = 0.05
    runner, world, printer, lay = make(machine, n=1)
    moving = {"n": 0}
    original = runner.vision.capture

    def jittery(name, height):  # a hand is still moving the part around
        moving["n"] += 1
        world.parts[0].x += 5.0 if moving["n"] % 2 else -5.0
        return original(name, height)

    runner.vision.capture = jittery  # type: ignore[method-assign]
    runner.command("auto")
    time.sleep(1.0)
    assert len(world.dropped) == 0  # never sorted while it moves
    runner.vision.capture = original  # type: ignore[method-assign]
    assert until(lambda: len(world.dropped) == 1)
    runner.command("stop")
    runner.wait(10)
    assert runner.state.status == "idle"


def P(cls, length, xy, pid):  # noqa: N802
    p = Part(pid, cls, 0.9, xy, 0.0, np.zeros((0, 2)), 4.0, length, True, False, length is not None)
    p.bed_xy, p.gap_mm = xy, 10.0
    return p


def test_largest_group_first(machine):
    pl = Planner(Layout.default(), machine, "sort")
    parts = [P("DIN912_M3", 8.0, (60.0, 60.0), 1)]  # nearest, but alone
    parts += [P("DIN934_M4", None, (150.0 + i, 150.0), 10 + i) for i in range(3)]
    assert pl.next(parts, here=(60.0, 60.0)).part.cls == "DIN934_M4"
    machine.run.order = "nearest"
    assert pl.next(parts, here=(60.0, 60.0)).part.cls == "DIN912_M3"


def test_tip_touches_plate(machine):
    runner, world, printer, lay = make(machine, n=0)
    printer.gcode("G28\nG1 X100 Y100 Z3.2")
    res = runner.tip_touches_plate()
    assert res["offset_z"] == round(lay.plate_thickness_mm - 3.2, 3)
    assert machine.tool.offset[2] == res["offset_z"]
    machine.tool.offset[2] = 0.0
    again, _, _, _ = make(machine, n=0)  # read back at start
    assert machine.tool.offset[2] == res["offset_z"]
    assert again.snapshot()["tip_offset_z"] == res["offset_z"]


def test_same_parts():
    a = [P("DIN912_M3", 8.0, (10.0, 10.0), 1), P("DIN934_M4", None, (50.0, 50.0), 2)]
    b = [P("DIN934_M4", None, (51.0, 49.0), 7), P("DIN912_M3", 8.0, (10.5, 10.0), 8)]
    assert same_parts(a, b)
    b[0].bed_xy = (60.0, 50.0)
    assert not same_parts(a, b)
    assert not same_parts(a, a[:1])
