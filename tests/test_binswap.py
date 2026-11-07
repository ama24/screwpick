"""Bin changes: which bin is asked for, and what the camera finds after the OK."""

from __future__ import annotations

import time
from collections import Counter

import pytest

from sortering_pnp.binswap import BinRequest, after_ok, choose_swap
from sortering_pnp.layout import Bin, Layout
from sortering_pnp.printer.sim import SimPart, SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


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
    assert runner.command("ok").startswith("parking")  # first OK: park
    _wait_for(runner, "parked")
    assert runner.snapshot()["swap_phase"] == "parked"
    if swap:
        seen = Layout.from_dict(lay.to_dict())
        seen.bins = [b for b in seen.bins if b.marker != 103] + [Bin(marker=135, x=3, y=0)]
        cam["layout"] = seen
    assert runner.command("ok").startswith("checking")  # second OK: bins changed
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
    assert runner.command("ok") == "change the bins, then press Change bin again"
    assert runner.command("ok") == "bin E1 emptied: it starts again"
    assert runner.requests == [] and 104 not in runner.bin_counts


def test_a_full_bin_does_not_stop_the_rest_and_ok_parks_mid_run(machine):
    machine.run.shake.enabled = False
    lay = _lay()
    path = machine.path(machine.paths.layout)
    lay.save(path)
    world = _world(lay)
    n = len(world.parts)
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    runner.claims[103] = "DIN934_M4"  # its bin is already full
    runner.bin_counts[103], runner.bin_volume[103] = 60, 1e6
    runner.vision.delay_s = 0.05  # time to press OK while it sorts
    runner.command("start", "sort")
    end = time.time() + 10
    while runner.state.picks < 1 and time.time() < end:
        time.sleep(0.01)
    assert runner.command("ok").startswith("parking")
    _wait_for(runner, "parked")
    assert world.parts and world.held is None  # parked between moves, parts still on the plate
    assert runner.command("ok").startswith("checking")
    _wait_for(runner, "done")
    assert len(world.dropped) == n  # all sorted: the nuts went on in another wild bin
    assert runner.requests == []  # the same code came back: bin D1 counts as emptied
    assert any("bin D1 emptied" in e for e in runner.events)


def test_full_button_learns_the_packing_and_asks_to_empty(machine):
    lay = _lay()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    runner.bin_counts[104], runner.bin_volume[104], runner.bin_family[104] = 20, 4000.0, "DIN934"
    before = runner.bin_fill(lay)[104]
    assert not before["full"] and before["packing"] == machine.run.fill.packing
    state = runner.bin_full(104)
    after = runner.bin_fill(lay)[104]
    assert after["full"] and after["share"] == pytest.approx(1.0) and after["packing"] < 0.3
    assert [r["marker"] for r in runner.snapshot()["requests"]] == [104]
    assert state["fill"]["104"]["full"]
    again = Runner(machine, SimPrinter(machine, world), runner.vision)  # kept across restarts
    assert again.packing.samples == runner.packing.samples


def test_ok_counts_parts_still_in_an_emptied_bin(machine):
    lay = _lay()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    runner.printer.gcode("G28")
    cx, cy = lay.bin_centre(lay.bin_by_marker(104))
    world.parts = [
        SimPart("DIN934_M4", cx - 8, cy - 8, 0.0, None, (7.0, 7.0), 3.2),
        SimPart("DIN934_M4", cx + 6, cy - 10, 0.0, None, (7.0, 7.0), 3.2),
    ]  # two nuts left in the bin
    runner.requests.append(BinRequest(104, "E1", "full", "DIN934_M4", 30))
    runner.bin_counts[104], runner.bin_volume[104] = 30, 3000.0
    runner.command("ok")
    assert runner.command("ok") == "bin E1 still has 2 part(s) in it: counted 2"
    assert runner.bin_counts[104] == 2 and runner.bin_volume[104] == pytest.approx(200.0)


def _running(machine, lay, cam):
    world = _world(lay)
    runner = Runner(
        machine,
        SimPrinter(machine, world),
        SimVision(world, lambda: cam["layout"], Transform2D.identity),
    )
    return runner, world


def test_the_check_after_ok_uses_a_new_photo(machine):
    """The photo from before parking must not be used to check the bins after the second OK
    (it would show the old bin: "emptied")."""
    machine.run.shake.enabled = False
    lay = _lay()
    lay.save(machine.path(machine.paths.layout))
    cam = {"layout": Layout.from_dict(lay.to_dict())}
    runner, world = _running(machine, lay, cam)
    runner.claims[103] = "DIN934_M4"
    runner.bin_counts[103], runner.bin_volume[103] = 60, 1e6  # full: asked for at once
    runner.vision.delay_s = 0.05
    runner.command("start", "sort")
    end = time.time() + 10
    while runner.state.picks < 1 and time.time() < end:
        time.sleep(0.01)
    runner.command("ok")
    _wait_for(runner, "parked")
    seen = Layout.from_dict(lay.to_dict())
    seen.bins = [b for b in seen.bins if b.marker != 103] + [Bin(marker=135, x=3, y=0)]
    cam["layout"] = seen  # bin 103 swapped for an empty one
    runner.command("ok")
    _wait_for(runner, "done")
    assert any("another bin stands at D1" in e for e in runner.events), runner.events[-8:]
    assert runner.bin_counts[103] == 60  # kept aside with its parts, not "emptied"


def test_bins_not_seen_during_a_run_stay_in_the_layout(machine):
    machine.run.shake.enabled = False
    lay = _lay()
    lay.save(machine.path(machine.paths.layout))
    seen = Layout.from_dict(lay.to_dict())
    seen.bins = [b for b in seen.bins if b.marker != 105]  # its code is covered
    cam = {"layout": seen}
    runner, _ = _running(machine, lay, cam)
    runner.bin_counts[104], runner.bin_volume[104] = 60, 1e6
    runner.claims[104] = "X"
    runner.command("start", "sort")
    _wait_for(runner, "waiting")  # 105 unseen, 104 full: not enough bins
    runner.command("ok")
    _wait_for(runner, "parked")
    runner.command("ok")  # the bin change is checked and the layout saved
    end = time.time() + 10
    while runner.requests and time.time() < end:
        time.sleep(0.05)
    runner.command("stop")
    runner.wait(10)
    saved = runner.layout()
    assert saved.bin_by_marker(105) is not None  # still there, with its settings
    assert saved.bin_by_marker(104) is not None  # the same code back: emptied, still there


def test_waiting_for_a_bin_still_sorts_new_parts(machine):
    machine.run.shake.enabled = False
    machine.standby.interval_s = 0.3  # look again soon
    lay = _lay()
    lay.bins = [b for b in lay.bins if b.marker != 105]
    lay.save(machine.path(machine.paths.layout))
    cam = {"layout": Layout.from_dict(lay.to_dict())}
    runner, world = _running(machine, lay, cam)
    runner.command("start", "sort")
    _wait_for(runner, "waiting")
    n = len(world.dropped)
    x0, y0, _, _ = lay.work_rect()
    world.parts.append(SimPart("DIN934_M4", x0 + 60, y0 + 70, 0.0, None, (7.0, 7.0), 3.2))
    end = time.time() + 15
    while len(world.dropped) == n and time.time() < end:
        time.sleep(0.05)
    assert len(world.dropped) == n + 1  # sorted without pressing OK
    _wait_for(runner, "waiting")
    runner.command("stop")
    runner.wait(10)


def test_requests_survive_a_restart(machine):
    lay = _lay()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    runner.bin_counts[104], runner.bin_volume[104] = 20, 4000.0
    runner.bin_full(104)
    again = Runner(machine, SimPrinter(machine, world), runner.vision)
    assert [(r.marker, r.why) for r in again.requests] == [(104, "full")]


def test_a_new_kind_with_no_bin_to_ask_for_goes_to_overflow(machine):
    lay = Layout.default()
    lay.bins = [b for b in lay.bins if b.marker in (102, 103)] + [lay.work_bin()]
    lay.bin_by_marker(103).role = "wild"  # one wild bin, and the overflow bin 102
    lay.save(machine.path(machine.paths.layout))
    world = _world(lay)
    world.parts = [p for p in world.parts if p.cls != "ISO7380_M4"]  # nuts and washers
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    runner.claims[103] = "DIN912_M3x8"  # taken by a kind no longer on the plate
    runner.printer.gcode("G28")
    shot = runner.take_shot(lay, Transform2D.identity())
    from sortering_pnp.strategies import Planner

    planner = Planner(lay, machine, "sort")
    planner.claims = runner.claims
    assert planner.next(shot.parts).kind == "wait_bin"  # nuts and washers both need a bin
    runner._ask_bins(planner, lay, shot.parts)
    assert [r.marker for r in runner.requests] == [103]  # one bin for one of them
    plan = planner.next(shot.parts)
    assert plan.kind == "pick" and plan.bin.marker == 102  # the other: into overflow
    assert any("into the overflow bin" in e for e in runner.events)
