"""Bin changes: which bin is asked for, and what the camera finds after the OK."""

from __future__ import annotations

import time
from collections import Counter

import pytest

from screwpick.binswap import BinRequest, after_ok, choose_swap
from screwpick.layout import Bin, Layout
from screwpick.printer.sim import SimPart, SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D


def _lay() -> Layout:
    lay = Layout.default()
    keep = {103, 104, 105}
    lay.bins = [b for b in lay.bins if b.marker in keep] + [lay.work_bin()]
    for b in lay.bins:
        if b.role != "work":
            b.role = "wild"
    return lay


def test_choose_swap_prefers_done_and_full_bins():
    lay = _lay()
    claims = {103: "A", 104: "B", 105: "C"}
    fill = {103: 0.3, 104: 0.8, 105: 0.95}
    on_plate = Counter({"C": 5, "B": 0, "A": 0})
    b, why = choose_swap(lay, claims, fill, on_plate, exclude=set())
    assert b.marker == 104 and "nothing on the plate" in why  # done and fuller than A
    b, _ = choose_swap(lay, claims, fill, on_plate, exclude={104})
    assert b.marker == 103  # C is fuller, but its parts are still on the plate
    assert choose_swap(lay, {}, fill, on_plate, set()) is None  # no claimed wild bin


@pytest.mark.parametrize(
    ("there", "outcome"), [("same", "emptied"), ("other", "replaced"), ("none", "removed")]
)
def test_after_ok(there, outcome):
    lay = _lay()
    b = lay.bin_by_marker(103)
    centre = lay.bin_centre(b)
    seen = {"same": [(103, centre, 0.0)], "other": [(135, centre, 0.0)], "none": []}[there]
    req = BinRequest(103, lay.bin_name(b), "full", "A", 20)
    assert after_ok(lay, req, seen) == outcome
    assert (lay.bin_by_marker(103) is None) == (outcome != "emptied")
    assert any(a.marker == 103 for a in lay.away) == (outcome != "emptied")


def _world(lay: Layout) -> SimWorld:
    world = SimWorld(seed=1)
    world.layout = lay
    world.plate_thickness = lay.surface_z()
    x0, y0, _, _ = lay.work_rect()
    spots = iter((x0 + 20 + 22 * i, y0 + 20 + 30 * j) for j in range(3) for i in range(4))
    kinds = [("DIN934_M4", None, (7.0, 7.0), 3.2)] * 3 + [("DIN125A_M4", None, (9.0, 9.0), 0.8)] * 2
    kinds += [("ISO7380_M4", 8.0, (10.0, 7.6), 2.2)]
    for cls, length, size, top in kinds:
        x, y = next(spots)
        world.parts.append(SimPart(cls, x, y, 0.0, length, size, top))
    return world


def _wait_for(runner: Runner, status: str, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while runner.state.status != status and time.time() < end:
        time.sleep(0.05)
    assert runner.state.status == status, runner.state.message


@pytest.mark.parametrize("swap", [True, False])
def test_a_new_kind_asks_for_a_bin_and_the_ok_checks_it(machine, swap):
    """Three wild bins, four kinds: the fourth waits; the bin of a finished kind is asked for.
    swap: another (empty) bin is put there; else the same bin comes back emptied."""
    machine.run.shake.enabled = False
    lay = _lay()
    lay.bins = [b for b in lay.bins if b.marker != 105]  # two wild bins, three kinds
    path = machine.path(machine.paths.layout)
    lay.save(path)
    world = _world(lay)
    cam = {"layout": Layout.from_dict(lay.to_dict())}
    runner = Runner(
        machine,
        SimPrinter(machine, world),
        SimVision(world, lambda: cam["layout"], Transform2D.identity),
    )
    runner.command("start", "sort")
    _wait_for(runner, "waiting")
    (req,) = runner.requests
    assert req.why == "new_kind" and req.for_kind == "ISO7380_M4x8"
    assert req.marker == 103 and req.what == "DIN934_M4" and req.count == 3  # done, fullest
    assert "ISO7380_M4x8 needs a bin: empty bin D1" in runner.state.message
    assert len(world.parts) == 1  # everything else is sorted
    if swap:
        seen = Layout.from_dict(lay.to_dict())
        seen.bins = [b for b in seen.bins if b.marker != 103] + [Bin(marker=135, x=3, y=0)]
        cam["layout"] = seen
    assert runner.command("ok").startswith("OK")
    _wait_for(runner, "done")
    saved = runner.layout()
    target = 135 if swap else 103
    assert saved.bin_by_marker(target) is not None
    assert runner.claims[target] == "ISO7380_M4x8" and runner.bin_counts[target] == 1
    if swap:  # the full bin is kept aside with what it holds
        assert [b.marker for b in saved.away] == [103]
        assert runner.claims[103] == "DIN934_M4" and runner.bin_counts[103] == 3
    assert runner.requests == []
    assert any("emptied" in e or "kept aside" in e for e in runner.events)


def test_ok_while_idle_checks_the_bins(machine):
    lay = _lay()
    path = machine.path(machine.paths.layout)
    lay.save(path)
    world = SimWorld(seed=1)
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    runner.printer.gcode("G28")  # the OK checks with a photo: the printer is homed
    runner.requests.append(BinRequest(104, "E1", "full", "B", 30))
    runner.bin_counts[104] = 30
    assert runner.command("ok") == "OK: bin E1 emptied: it starts again"
    assert runner.requests == [] and 104 not in runner.bin_counts
