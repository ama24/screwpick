"""Pick -> photo with the part in the picker -> drop while the photo is analysed."""

from __future__ import annotations

import time

from sortering_pnp.layout import Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


def make(machine, n: int = 5):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=4)
    world.populate(lay, n=n)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    printer = SimPrinter(machine, world)
    return Runner(machine, printer, vision), world, printer, vision


def test_photo_is_taken_with_the_part_in_the_picker(machine):
    machine.run.shake.enabled = False
    runner, world, printer, vision = make(machine)
    n = len(world.parts)
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done", runner.state.message
    assert len(world.dropped) == n
    held = [h for h in vision.snaps if h is not None]
    assert len(held) == n  # one photo per pick, each with that part in the picker
    assert held == [sp for sp, _ in world.dropped]
    # the photo comes between grab and release
    log = printer.log
    grab = log.index(machine.picker.grab)
    assert log.index(machine.picker.release, grab) > grab


def test_analysis_runs_while_the_part_is_carried(machine):
    machine.run.shake.enabled = False
    runner, world, printer, vision = make(machine, n=2)
    vision.delay_s = 0.4  # the Pi thinking
    gcode = printer.gcode

    def slow_drop(script: str) -> None:  # the bed travelling to the bin
        gcode(script)
        if machine.picker.release in script:
            time.sleep(0.4)

    printer.gcode = slow_drop  # type: ignore[method-assign]
    runner.command("step")
    runner.wait(30)
    t = runner.state.timing
    assert t, "no pipeline timing recorded"
    assert t["waited_for_analysis_s"] < 0.2  # the 0.4 s analysis hid behind the 0.4 s drop
    assert t["cycle_s"] < 0.75


def test_pipeline_off_picks_then_photographs(machine):
    machine.run.pipeline = False
    machine.run.shake.enabled = False
    runner, world, printer, vision = make(machine, n=3)
    runner.command("start")
    runner.wait(60)
    assert runner.state.status == "done" and not world.parts
    assert all(h is None for h in vision.snaps)  # photos only with an empty picker
