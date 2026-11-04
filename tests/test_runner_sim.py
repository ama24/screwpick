"""End to end with the simulated printer, parts and camera."""

from __future__ import annotations

import json
import threading
import urllib.request

from screwpick.cli import main
from screwpick.layout import Layout
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import BedCalibration, Transform2D


def make(machine, layout: Layout, n: int = 10, seed: int = 3):
    lay_path = machine.path(machine.paths.layout)
    layout.save(lay_path)
    world = SimWorld(seed=seed)
    world.populate(layout, n=n)
    vision = SimVision(world, lambda: Layout.load(lay_path), Transform2D.identity)
    return Runner(machine, SimPrinter(machine, world), vision), world


def test_sorts_everything_into_the_right_bins(machine, layout):
    machine.run.shake.enabled = False
    runner, world = make(machine, layout)
    n = len(world.parts)
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done", runner.state.message
    assert not world.parts and world.held is None
    assert len(world.dropped) == n
    for sp, xy in world.dropped:  # each part landed inside the bin its rule names
        b = next(b for b in layout.bins if b.marker == _bin_at(layout, xy))
        if sp.cls == "UNKNOWN":
            assert b.role == "unknown"
        elif sp.cls == "DIN912_M3":
            assert b.holds == ["DIN912_M3"]
        elif sp.cls == "DIN912_M4":
            assert b.holds == (["DIN912_M4x16"] if sp.length == 16 else ["DIN912_M4"])
        elif sp.cls in ("DIN934_M4", "DIN125A_M4"):
            assert b.marker == 106
        else:
            assert b.holds == ["*"]
    assert sum(runner.state.counts.values()) == n


def _bin_at(layout: Layout, xy) -> int:
    for b in layout.bins:
        x0, y0, x1, y1 = layout.cells_rect(b.x, b.y, b.w, b.d)
        if x0 <= xy[0] <= x1 and y0 <= xy[1] <= y1:
            return b.marker
    raise AssertionError(f"{xy} is in no bin")


def test_step_does_one_pick_and_bad_layout_is_an_error(machine, layout):
    runner, world = make(machine, layout)
    n = len(world.parts)
    runner.command("step")
    runner.wait(30)
    assert len(world.parts) == n - 1 and runner.state.picks == 1
    layout.bins[0].x, layout.bins[0].y = 2, 2  # into the working area
    layout.save(machine.path(machine.paths.layout))
    runner.command("start")
    runner.wait(30)
    assert runner.state.status == "error" and "overlaps" in runner.state.message


def test_failed_picks_are_detected(machine, layout):
    machine.run.shake.enabled = False
    machine.run.max_failed_picks = 2
    runner, world = make(machine, layout, n=1)
    world.grab_radius_mm = 0.0  # the picker always misses
    world.parts[0].x += 0.5
    runner.command("start")
    runner.wait(30)
    assert runner.state.failed_picks == 2
    assert runner.state.status == "stuck"
    runner.command("stop")
    runner.wait(10)


def test_calibration_scan_and_goto(machine, layout):
    runner, world = make(machine, layout, n=0)
    runner.setup_home()  # setup moves never home by themselves
    markers = runner.calib_photo()
    assert {m["marker"] for m in markers} == {b.marker for b in layout.bins}
    # jog the tip onto three markers: in the simulation the plate frame = printer frame
    for m in markers[:3]:
        x, y = m["plate"]
        runner.motion.travel(x, y)
        runner.calib_record(m["marker"])
    fit = runner.calib_fit()
    assert max(fit["residuals_mm"]) < 1e-6
    cal = BedCalibration.load(runner.transform_path)
    assert cal is not None and len(cal.pairs) == 3
    tip = runner.goto_marker(markers[0]["marker"])
    assert tip[:2] == markers[0]["plate"]
    real = runner.layout()
    runner.vision.layout = lambda: real  # what the (simulated) camera sees
    lay = runner.layout()
    b = lay.bin_by_marker(105)
    b.x, b.y = 1, 4  # moved in the file, but the camera sees it at (0, 4)
    lay.bins = [x for x in lay.bins if x.marker != 106]  # make room
    lay.save(runner.layout_path)
    res = runner.scan_bins()
    assert res["problems"] == []
    assert runner.layout().bin_by_marker(105).x == 0


def test_web_api(machine, layout):
    from screwpick.web.server import make_server

    runner, world = make(machine, layout)
    srv = make_server(runner, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.read(), r.headers.get("Content-Type")

    def post(path, body):
        req = urllib.request.Request(
            base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    try:
        page, ctype = get("/")
        assert b"screwpick" in page and ctype.startswith("text/html")
        state = json.loads(get("/api/state")[0])
        assert state["status"] == "idle" and "sort" in state["strategies"]
        lay = json.loads(get("/api/layout")[0])
        assert lay["problems"] == [] and len(lay["layout"]["bins"]) == 16
        lay["layout"]["bins"][0]["holds"] = ["DIN933*"]
        saved = post("/api/layout", lay)
        assert saved["layout"]["bins"][0]["holds"] == ["DIN933*"]
        assert post("/api/setup/home", {})["homed_now"] is True
        assert post("/api/jog", {"dx": 0, "dy": 0, "dz": 1})["tip"][2] > 0
        data, ctype = get("/api/print/labels")
        assert data.startswith(b"<svg") and ctype == "image/svg+xml"
        try:
            urllib.request.urlopen(base + "/runs/../layout.yaml", timeout=5)
            raise AssertionError("path traversal must fail")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        assert post("/api/command", {"command": "step"})["result"].startswith("step")
        runner.wait(30)
        assert runner.state.picks == 1
    finally:
        srv.shutdown()


def test_cli_run_sim(machine, layout, tmp_path, capsys):
    cfg = tmp_path / "machine.yaml"
    layout.save(tmp_path / "layout.yaml")
    cfg.write_text(
        "paths: {layout: layout.yaml, transform: t.json, runs: runs}\n"
        "printer: {driver: sim}\nrun: {shake: {enabled: false}}\n",
        encoding="utf-8",
    )
    assert main(["--config", str(cfg), "run", "--strategy", "sort"]) == 0
    out = capsys.readouterr().out
    assert "done: the working area is empty" in out
