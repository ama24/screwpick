"""Automatic mode (standby, look, sort, wait), homing over the plate, largest group first, tip height."""

from __future__ import annotations

import time

import numpy as np

from sortering_pnp.layout import Layout
from sortering_pnp.parts import Part
from sortering_pnp.printer.sim import SimPart, SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner, same_parts
from sortering_pnp.strategies import Planner
from sortering_pnp.transform import Transform2D


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
        f"SET_GCODE_VARIABLE MACRO=_SORTERING_HOME VARIABLE=z_home_x VALUE={cx:.2f}" in printer.log
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
    original = runner.vision.snap

    def jittery(name):  # a hand is still moving the part around
        moving["n"] += 1
        world.parts[0].x += 5.0 if moving["n"] % 2 else -5.0
        return original(name)

    runner.vision.snap = jittery  # type: ignore[method-assign]
    runner.command("auto")
    time.sleep(1.0)
    assert len(world.dropped) == 0  # never sorted while it moves
    runner.vision.snap = original  # type: ignore[method-assign]
    assert until(lambda: len(world.dropped) == 1)
    runner.command("stop")
    runner.wait(10)
    assert runner.state.status == "idle"


def P(cls, length, xy, pid):  # noqa: N802
    p = Part(pid, cls, 0.9, xy, 0.0, np.zeros((0, 2)), 4.0, length, True, False, length is not None)
    p.bed_xy, p.gap_mm = xy, 10.0
    return p


def test_census_order_finishes_a_kind_and_goes_back_to_one_that_turns_up(machine):
    pl = Planner(Layout.default(), machine, "sort")
    a = [P("DIN934_M4", None, (150.0 + 3 * i, 150.0), 10 + i) for i in range(3)]
    b = [P("DIN912_M3", 8.0, (60.0 + 3 * i, 60.0), 20 + i) for i in range(2)]
    c = [P("DIN125A_M4", None, (90.0, 90.0), 30)]
    plan = pl.next(a + b + c, here=(60.0, 60.0))
    assert plan.part.cls == "DIN934_M4" and plan.note.startswith("next: DIN934_M4")
    # one A left, but more B: still A (it is being finished)
    plan = pl.next(a[:1] + b + b + c, here=(60.0, 60.0))
    assert plan.part.cls == "DIN934_M4" and plan.note == ""
    plan = pl.next(b + c, here=(150.0, 150.0))  # A done: the biggest of the rest
    assert plan.part.cls == "DIN912_M3" and plan.note.startswith("next: DIN912_M3 ")
    plan = pl.next(b[:1] + c + a[:1], here=(60.0, 60.0))  # an A was dug out: back to A
    assert plan.part.cls == "DIN934_M4" and plan.note.startswith("back to DIN934_M4")
    plan = pl.next(b[:1] + c, here=(60.0, 60.0))  # then on with B, before the new kind C
    assert plan.part.cls == "DIN912_M3" and plan.note.startswith("on with DIN912_M3 ")
    assert pl.next(c, here=(60.0, 60.0)).part.cls == "DIN125A_M4"
    assert pl.started == ["DIN934_M4", "DIN912_M3", "DIN125A_M4"]


def test_census_groups_by_class_until_the_length_is_known(machine):
    pl = Planner(Layout.default(), machine, "sort")
    m4 = [P("DIN912_M4", 16.0, (150.0 + 3 * i, 150.0), 10 + i) for i in range(2)]
    m4.append(P("DIN912_M4", None, (160.0, 150.0), 12))  # not measured yet
    nuts = [P("DIN934_M4", None, (60.0 + 3 * i, 60.0), 20 + i) for i in range(2)]
    plan = pl.next(m4 + nuts, here=(60.0, 60.0))
    assert plan.part.cls == "DIN912_M4" and pl.started == ["DIN912_M4"]  # 3 of them, not 2 + 1


def test_largest_group_first(machine):
    machine.run.order = "largest_group"
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
    assert res["offset_z"] == round(lay.surface_z() - 3.2, 3)  # tray floor + TPU sheet
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


def test_same_parts_allows_the_model_to_flicker():
    """A real model: a class changes, a part is missed in one photo: still lying still."""
    a = [P("DIN912_M3", 8.0, (10.0 * i, 10.0), i) for i in range(1, 11)]
    b = [
        P("UNKNOWN" if i == 3 else "DIN912_M4", 8.0, (10.0 * i + 1, 10.5), i) for i in range(1, 10)
    ]
    assert same_parts(a, b)  # 10 then 9, classes changed, positions within a mm
    moved = [P(p.cls, 8.0, (p.bed_xy[0], p.bed_xy[1] + 20), 0) for p in a]
    assert not same_parts(a, moved)  # a hand swept them along


def test_resume_in_automatic_mode_sorts_now(machine):
    """Parts that never lie still (or a model that flickers): Resume sorts them at once."""
    machine.standby.interval_s = 60.0
    machine.standby.settle_s = 60.0
    machine.run.shake.enabled = False
    runner, world, printer, lay = make(machine, n=2)
    runner.command("auto")
    assert until(lambda: "lie still" in runner.state.message, 10)
    assert runner.command("resume") == "looking now"
    assert until(lambda: not world.parts, 30)  # sorted, without waiting 60 s
    assert runner.command("start") == "looking now"  # Start in standby does the same
    runner.command("stop")
