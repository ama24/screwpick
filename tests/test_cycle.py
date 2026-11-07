"""One sorting cycle: photo (parked, picker empty) -> pick -> drop -> park -> next photo."""

from __future__ import annotations

import time

from sortering_pnp import config as config_mod
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


def _log_photos(printer: SimPrinter, vision: SimVision) -> None:
    """Each photo leaves a line in the printer's log: where the head was, what it held."""
    snap = vision.snap

    def logged(name):  # noqa: ANN001, ANN202
        x, y = printer.pos[:2]
        held = "held" if vision.world.held is not None else "empty"
        printer.log.append(f"; photo X{x:.1f} Y{y:.1f} {held}")
        return snap(name)

    vision.snap = logged  # type: ignore[method-assign]


def test_photo_pick_drop_park_photo(machine):
    machine.run.shake.enabled = False
    machine.run.scan_bins_on_start = machine.run.only_seen_bins = False  # no bins photo first
    runner, world, printer, vision = make(machine, n=2)
    _log_photos(printer, vision)
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done", runner.state.message
    assert len(world.dropped) == 2
    px, py = machine.printer.photo_position[:2]
    parked = f"; photo X{px:.1f} Y{py:.1f} empty"
    steps = [
        line
        for line in printer.log
        if line.startswith("; photo") or line in ("; pins grab", "; pins release")
    ]
    # every photo parked, picker empty; two cycles and the last look (plate empty: done)
    expect = [parked, "; pins grab", "; pins release"] * 2 + [parked]
    assert steps == expect, steps
    assert all(h is None for h in vision.snaps)
    assert runner.state.timing["cycle_s"] >= 0


def test_a_failed_pick_is_seen_on_the_next_photo(machine):
    machine.run.shake.enabled = False
    machine.run.max_failed_picks = 1
    runner, world, printer, vision = make(machine, n=1)
    world.grab_radius_mm = 0.0  # the picker misses
    world.parts[0].x += 0.5
    runner.command("start")
    end = time.time() + 30
    while runner.state.status != "stuck" and time.time() < end:
        time.sleep(0.05)
    assert runner.state.failed_picks == 1 and runner.state.status == "stuck"
    assert any("missed" in line for line in runner.console)
    runner.command("stop")
    runner.wait(10)


def test_retired_settings_still_load(tmp_path):
    p = tmp_path / "machine.yaml"
    p.write_text("run:\n  pipeline: true\nstandby:\n  settle_s: 8\n", encoding="utf-8")
    m = config_mod.load(p)
    assert not hasattr(m.run, "pipeline") and not hasattr(m.standby, "settle_s")
