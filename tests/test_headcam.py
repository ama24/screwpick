"""A camera on the tool head: photos over an area, joined in the calibration photo's frame."""

from __future__ import annotations

import math

import numpy as np

from screwpick.headcam import merge_parts, tiles
from screwpick.layout import Bin, Layout
from screwpick.parts import Part
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import BedCalibration, Transform2D


def test_tiles_cover_the_area_in_a_zigzag():
    rect = (10.0, 20.0, 180.0, 120.0)
    fov = (80.0, 60.0)
    spots = tiles(rect, fov, 0.25)
    assert len(spots) == 3 * 2
    assert spots[0][1] == spots[2][1] and spots[2][0] == spots[3][0]  # zigzag: short moves
    for x in np.linspace(rect[0], rect[2], 25):
        for y in np.linspace(rect[1], rect[3], 25):
            assert any(abs(x - sx) <= 40 and abs(y - sy) <= 30 for sx, sy in spots)
    assert tiles((0, 0, 50, 40), fov, 0.25) == [(25.0, 20.0)]  # fits in one photo


def test_merge_parts_keeps_one_of_each():
    def p(cls, xy, score):
        return Part(0, cls, score, xy, 0.0, np.zeros((0, 2)), 3.0)

    parts = [p("A", (10, 10), 0.7), p("A", (11, 10.5), 0.9), p("B", (10.5, 10), 0.8)]
    parts.append(p("A", (30, 10), 0.6))
    kept = merge_parts(parts, 3.0)
    assert len(kept) == 3 and next(q for q in kept if q.plate_xy == (11, 10.5)).score == 0.9


def _head_runner(machine, layout: Layout, cam_layout: Layout, T: Transform2D, n: int):
    machine.camera.source = "head"
    machine.camera.head.fov_mm = [80.0, 60.0]
    machine.camera.head.offset = [12.0, -8.0]
    machine.run.shake.enabled = False
    x0, y0, x1, y1 = layout.work_rect()
    ref = ((x0 + x1) / 2 - 12.0, (y0 + y1) / 2 + 8.0)  # the camera over the tray's middle
    machine.printer.photo_position = [ref[0], ref[1], 60.0]
    layout.save(machine.path(machine.paths.layout))
    BedCalibration(T, [], []).save(machine.path(machine.paths.transform))
    world = SimWorld(seed=7)
    world.populate(layout, n=n)
    printer = SimPrinter(machine, world)
    vision = SimVision(world, lambda: cam_layout, lambda: T)
    vision.head_tip = printer._tip  # noqa: SLF001
    vision.head = machine.camera.head
    vision.head_ref = ref
    return Runner(machine, printer, vision), world, vision


def _turned() -> Transform2D:
    a = math.radians(3.0)  # the camera is a little turned and off: the calibration knows
    return Transform2D(
        np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]), np.array([4.0, -2.5])
    )


def test_sorting_with_the_head_camera(machine, layout):
    runner, world, vision = _head_runner(machine, layout, layout, _turned(), n=10)
    n = len(world.parts)
    runner.command("start", "sort")
    runner.wait(120)
    assert runner.state.status == "done", runner.state.message
    assert not world.parts and len(world.dropped) == n  # each picked once: no doubles
    rects = [layout.cells_rect(b.x, b.y, b.w, b.d) for b in layout.bins if b.role != "work"]
    for _, (x, y) in world.dropped:  # every drop landed in a bin
        assert any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in rects)
    assert len(vision.snaps) > 2 * runner.state.picks  # several photos per look


def test_resync_with_the_head_camera_finds_bins_beside_the_grid(machine, layout):
    real = Layout.from_dict(layout.to_dict())
    real.bins.append(Bin(marker=135, x=5, y=2))  # put next to the 5x5 grid
    runner, _, _ = _head_runner(machine, layout, real, _turned(), n=0)
    runner.setup_home()
    res = runner.scan_bins()
    assert res["problems"] == []
    saved = runner.layout()
    assert saved.bin_by_marker(135) is not None and (saved.bin_by_marker(135).x, saved.cols) == (
        5,
        6,
    )
    assert {(b.marker, b.x, b.y) for b in saved.bins} == {(b.marker, b.x, b.y) for b in real.bins}
