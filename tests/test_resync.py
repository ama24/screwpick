"""Resync: the camera decides where the grid lies, which bins stand where and which way round,
and which one is the plate (the biggest, unless chosen by hand)."""

from __future__ import annotations

import pytest

from screwpick.layout import Bin, Layout
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D


def truth() -> Layout:
    """A 4x4 tray, a turned 1x2 bin and small bins, on a grid lying at an odd spot."""
    lay = Layout(origin=[23.4, 11.7], cols=6, rows=5)
    lay.bins = [
        Bin(marker=230, x=2, y=0, w=4, d=4),  # the 4x4 tray
        Bin(marker=140, x=0, y=0, w=2, d=1, rot=90),  # 1x2, lying across, shelf to the left
        Bin(marker=100, x=0, y=1),
        Bin(marker=101, x=5, y=4, rot=180),  # shelf to the front
        Bin(marker=102, x=0, y=4, rot=270),
    ]
    return lay


def seen_from(lay: Layout) -> list[tuple[int, tuple[float, float], float]]:
    return [(b.marker, lay.bin_centre(b), float(b.rot)) for b in lay.bins]


def test_resync_finds_the_grid_bins_turns_and_the_plate():
    real = truth()
    lay = Layout.default()  # what the program believed: grid at (5, 5), 3x3 tray 220
    report = lay.resync(seen_from(real))
    assert lay.origin == pytest.approx([23.4 + 0.0, 11.7])  # min cells are 0: same origin
    assert (lay.cols, lay.rows) == (6, 5)
    for b in real.bins:
        got = lay.bin_by_marker(b.marker)
        assert (got.x, got.y, got.w, got.d, got.rot) == (b.x, b.y, b.w, b.d, b.rot), b.marker
    assert lay.work_marker == 230 and lay.bin_by_marker(230).role == "work"  # the biggest
    assert lay.bin_by_marker(220) is None and 220 in report["removed"]  # the old tray is gone
    assert sorted(report["found"]) == [100, 101, 102, 140, 230]
    assert lay.problems() == []


def test_a_plate_chosen_by_hand_is_kept_while_it_is_there():
    real = truth()
    lay = Layout.default()
    lay.resync(seen_from(real))
    lay.set_work(140, auto=False)  # the user wants the 1x2 bin as the plate
    assert lay.bin_by_marker(230).role == "wild" and lay.bin_by_marker(140).role == "work"
    lay.resync(seen_from(real))
    assert lay.work_marker == 140 and not lay.work_auto
    real.bins = [b for b in real.bins if b.marker != 140]  # taken off the bed
    lay.resync(seen_from(real))
    assert lay.work_marker == 230 and lay.work_auto  # back to the biggest


def test_bins_taken_away_keep_their_rules():
    real = truth()
    lay = Layout()
    lay.resync(seen_from(real))
    b = lay.bin_by_marker(100)
    b.role, b.holds = "sort", ["DIN912_M3"]
    gone = Layout.from_dict(real.to_dict())
    gone.bins = [x for x in gone.bins if x.marker != 100]
    lay.resync(seen_from(gone))
    assert lay.bin_by_marker(100) is None and [a.marker for a in lay.away] == [100]
    lay.resync(seen_from(real))  # it is back
    back = lay.bin_by_marker(100)
    assert back.role == "sort" and back.holds == ["DIN912_M3"] and lay.away == []


@pytest.mark.parametrize(
    ("rot", "side"), [(0, "back"), (90, "left"), (180, "front"), (270, "right")]
)
def test_the_plate_area_leaves_out_the_shelf(rot, side):
    lay = Layout(origin=[0.0, 0.0], cols=3, rows=3)
    lay.bins = [Bin(marker=220, x=0, y=0, w=3, d=3, rot=rot)]
    lay.set_work(220, auto=True)
    x0, y0, x1, y1 = lay.work_rect()
    inner = 0.25 + 1.2
    full = (inner, inner, 126 - inner, 126 - inner)
    cut = {"back": (3, -14.0), "left": (0, 14.0), "front": (1, 14.0), "right": (2, -14.0)}[side]
    expected = list(full)
    expected[cut[0]] += cut[1]
    assert (x0, y0, x1, y1) == pytest.approx(tuple(expected))


def test_resync_through_the_camera(machine):
    real = truth()
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld()
    vision = SimVision(world, lambda: real, Transform2D.identity)
    runner = Runner(machine, SimPrinter(machine, world), vision)
    runner.setup_home()
    res = runner.scan_bins()
    assert res["problems"] == [] and res["work"] == 230
    saved = runner.layout()
    assert saved.origin == pytest.approx(real.origin, abs=0.05)
    assert {(b.marker, b.x, b.y, b.rot) for b in saved.bins} == {
        (b.marker, b.x, b.y, b.rot) for b in real.bins
    }
    assert runner.setup.is_done("layout")
