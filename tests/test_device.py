"""The Device tab's data: the photo to draw on and where the parts are on it, the part picked
next, the activity, bins changed from the page, and layout changes queued while sorting."""

from __future__ import annotations

import time

import numpy as np

from sortering_pnp.layout import Bin, Layout
from sortering_pnp.printer.sim import SimPart, SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


def _runner(machine, n=6, out=True):
    machine.run.shake.enabled = False
    lay = Layout.default()
    path = machine.path(machine.paths.layout)
    lay.save(path)
    world = SimWorld(seed=3)
    world.populate(lay, n=n)
    vision = SimVision(
        world,
        lambda: Layout.load(path),
        Transform2D.identity,
        machine.path("runs") if out else None,
    )
    return Runner(machine, SimPrinter(machine, world), vision), world


def _map(H, x, y):
    v = np.asarray(H) @ np.array([x, y, 1.0])
    return v[:2] / v[2]


def test_the_photo_the_parts_on_it_and_the_next_part(machine):
    runner, world = _runner(machine)
    runner.command("step", "sort")
    runner.wait(30)
    s = runner.snapshot()
    assert s["view"].endswith("_view.jpg") and s["view_h"]
    import cv2

    img = cv2.imread(str(machine.path("runs") / s["view"]))
    h, w = img.shape[:2]
    for p in s["parts"]:  # every outline lands inside the picture
        assert p["poly"] and p["id"]
        u, v = _map(s["view_h"], *p["xy"])
        assert 0 <= u < w and 0 <= v < h
    t = s["target"]
    assert t is not None and t["name"] and t["cell"] and len(t["poly"]) >= 3
    text = "\n".join(s["console"])
    assert "taking a photo" in text and "running the model" in text and "leaving " in text
    assert s["phase"]


def test_change_bin_asked_from_the_page(machine):
    runner, _ = _runner(machine, n=0, out=False)
    state = runner.ask_change(104)
    (req,) = runner.requests
    assert req.why == "asked" and req.cell == "E1" and "to be changed" in req.text()
    assert req.short() == "Change bin E1"
    assert runner.command("change_bin").startswith("change the bins")  # the page's button
    assert state["claims"] is not None


def test_layout_changes_are_used_from_the_next_pick(machine):
    runner, world = _runner(machine, n=0, out=False)
    lay = runner.layout()
    x0, y0, _, _ = lay.work_rect()
    world.parts = [
        SimPart("DIN934_M4", x0 + 30 + 20 * i, y0 + 40, 0.0, None, (7.0, 7.0), 3.2)
        for i in range(3)
    ]
    runner.vision.delay_s = 0.3  # time to change the layout while it sorts
    runner.command("start", "sort")
    end = time.time() + 10
    while runner.state.picks < 1 and time.time() < end:
        time.sleep(0.02)
    new = runner.layout()
    b = new.bin_by_marker(109)
    b.role, b.holds = "sort", ["DIN934_M4"]  # queued on the page, applied now
    runner.save_layout(new)
    runner.wait(30)
    assert runner.state.status == "done"
    assert any("layout changes applied" in e for e in runner.events)
    cx, cy = runner.layout().bin_centre(runner.layout().bin_by_marker(109))
    assert any(abs(x - cx) < 1 and abs(y - cy) < 1 for _, (x, y) in world.dropped)


def test_a_layout_with_problems_is_refused_while_sorting(machine):
    runner, world = _runner(machine, n=3, out=False)
    runner.vision.delay_s = 0.3
    runner.command("start", "sort")
    bad = runner.layout()
    bad.bins.append(Bin(marker=100, x=4, y=4))  # the same code twice
    try:
        runner.save_layout(bad)
        raise AssertionError("should be refused")
    except ValueError as exc:
        assert "not applied while sorting" in str(exc)
    runner.command("stop")
    runner.wait(30)
