"""The Setup tab: nothing sorts until every step is checked; each step's own guards."""

from __future__ import annotations

import pytest

from sortering_pnp.layout import Layout
from sortering_pnp.printer import PrinterError
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import BedCalibration


def make(machine, n: int = 3):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=2)
    world.populate(lay, n=n)
    transform_path = machine.path(machine.paths.transform)

    def transform():
        cal = BedCalibration.load(transform_path)
        from sortering_pnp.transform import Transform2D

        return cal.transform if cal else Transform2D.identity()

    vision = SimVision(world, lambda: lay, transform)
    printer = SimPrinter(machine, world)
    runner = Runner(machine, printer, vision)
    runner.require_setup = True  # as on a real printer
    return runner, world, printer


def test_nothing_sorts_before_the_setup(machine):
    runner, world, printer = make(machine)
    for cmd in ("start", "step", "auto"):
        assert runner.command(cmd).startswith("finish the Setup tab first: Printer, Homing")
    assert printer.log == []  # not a single command reached the printer
    with pytest.raises(PrinterError, match="home first"):
        runner.jog(0, 0, 1)  # setup moves never home by themselves


def test_the_whole_setup_then_sorting(machine):
    machine.run.shake.enabled = False
    runner, world, printer = make(machine)
    # 1 printer: questions only
    res = runner.setup_check_printer()
    assert res["ok"] and all(c["ok"] for c in res["checks"])
    # 2 homing: watched, then confirmed
    with pytest.raises(PrinterError, match="Home now"):
        runner.setup_confirm("homing")
    runner.setup_home()
    assert "G28" in printer.log
    runner.setup_confirm("homing")
    # 3 machine: values checked, shown, confirmed
    with pytest.raises(ValueError, match="minimum"):
        runner.save_machine({"limits_x": [220, 0]})
    with pytest.raises(PrinterError, match="limits"):
        runner.save_machine({"photo_position": [0, 300, 60]})
    assert runner.m.printer.photo_position == [0.0, 200.0, 60.0]  # unchanged after a refusal
    runner.save_machine({"photo_position": [0, 210, 60], "travel_z": 55})
    assert runner.setup_goto("photo")[:2] == [0.0, 210.0]
    runner.setup_goto("centre")
    runner.setup_confirm("machine")
    # 4 tip height: checks itself
    printer.gcode("G1 X100 Y100 Z3")
    runner.tip_touches_plate()
    # 5 picker: both tried first
    with pytest.raises(PrinterError, match="Grab and Release"):
        runner.setup_confirm("picker")
    runner.setup_picker("grab")
    runner.setup_picker("release")
    runner.setup_confirm("picker")
    # 6 camera: the calibration checks itself
    with pytest.raises(PrinterError, match="checked by the machine"):
        runner.setup_confirm("camera")
    markers = runner.calib_photo()
    for m in markers[:3]:
        runner.motion.travel(*m["plate"])
        runner.calib_record(m["marker"])
    runner.calib_fit()
    # 7 layout: bins found
    assert runner.scan_bins()["problems"] == []
    state = runner.setup_state()
    assert state["complete"], [s["title"] for s in state["steps"] if not s["done"]]
    assert runner.command("start").startswith("start")
    runner.wait(60)
    assert runner.state.status == "done" and not world.parts


def test_machine_values_are_kept_and_reset_the_step(machine):
    runner, _, _ = make(machine)
    runner.setup.mark("printer")
    runner.setup.mark("machine")
    runner.save_machine({"limits_y": [0, 230], "photo_position": [0, 230, 60]})
    assert not runner.setup.is_done("machine")  # changed: confirm again
    again, _, _ = make(machine)
    assert again.m.printer.limits_y == [0.0, 230.0]
    assert again.m.printer.photo_position == [0.0, 230.0, 60.0]
    assert again.setup.is_done("printer")  # the checklist is kept too


def test_a_bad_calibration_or_layout_unchecks_its_step(machine):
    runner, _, _ = make(machine)
    runner.setup.mark("camera")
    runner.calib_pairs = [
        {"marker": 100, "plate": [0, 0], "bed": [10, 10]},
        {"marker": 101, "plate": [100, 0], "bed": [110, 10]},
        {"marker": 102, "plate": [0, 100], "bed": [10, 113]},  # 3 mm off
    ]
    runner.calib_fit()
    assert not runner.setup.is_done("camera")


def test_setup_web_api(machine):
    import json
    import threading
    import urllib.request

    from sortering_pnp.web.server import make_server

    runner, _, _ = make(machine)
    srv = make_server(runner, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(
            base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    try:
        with urllib.request.urlopen(base + "/api/setup", timeout=10) as r:
            state = json.loads(r.read())
        assert state["required"] and not state["complete"] and len(state["steps"]) == 7
        assert post("/api/setup/printer", {})["ok"] is True
        assert post("/api/setup/home", {})["homed_now"] is True
        assert post("/api/setup/confirm", {"step": "homing"})["steps"][1]["done"] is True
        assert post("/api/setup/reset", {"step": "homing"})["steps"][1]["done"] is False
        assert post("/api/command", {"command": "start"})["result"].startswith("finish the Setup")
    finally:
        srv.shutdown()
