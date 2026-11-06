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
        assert runner.command(cmd).startswith(
            "finish the Setup tab first: Printer, Homing, Machine, Picker, Tip height"
        )
    assert printer.log == []  # not a single command reached the printer
    with pytest.raises(PrinterError, match="home first"):
        runner.jog(0, 0, 1)  # setup moves never home by themselves


def test_the_whole_setup_then_sorting(machine):
    machine.run.shake.enabled = False
    runner, world, printer = make(machine)
    printer.gcode("TOOL_HOTEND")  # the picker is not on yet: the setup moves need no picker
    printer.log.clear()
    # 1 printer: questions only
    res = runner.setup_check_printer()
    assert res["ok"] and all(c["ok"] for c in res["checks"])
    # 2 homing: watched, then confirmed
    with pytest.raises(PrinterError, match="Home now"):
        runner.setup_confirm("homing")
    runner.setup_home()
    assert "G28" in printer.log
    runner.setup_confirm("homing")
    # 3 machine: values checked, shown, confirmed; then the head waits for the picker
    with pytest.raises(ValueError, match="minimum"):
        runner.save_machine({"limits_x": [220, 0]})
    with pytest.raises(PrinterError, match="limits"):
        runner.save_machine({"photo_position": [0, 300, 60]})
    with pytest.raises(PrinterError, match="limits"):
        runner.save_machine({"change_position": [110, 210, 500]})
    assert runner.m.printer.photo_position == [0.0, 200.0, 60.0]  # unchanged after a refusal
    runner.save_machine(
        {"photo_position": [0, 210, 60], "change_position": [110, 200, 90], "travel_z": 55}
    )
    assert runner.setup_goto("photo")[:2] == [0.0, 210.0]
    assert runner.setup_goto("change") == [110.0, 200.0, 90.0]
    runner.setup_goto("centre")
    runner.setup_confirm("machine")
    assert runner.motion.current_tip() == [110.0, 200.0, 90.0]  # easy to put the picker on
    # 4 picker: one OK, which tells Klipper the picker is on
    assert runner.setup_state()["picker"]["pins"]["ok"]
    runner.setup_confirm("picker")
    assert printer.log[-1] == "TOOL_PICKER" and runner.setup.is_done("picker")
    # 5 tip height: checks itself
    printer.gcode("G1 X100 Y100 Z3")
    runner.tip_touches_plate()
    # 6 camera: the codes are read without any model, the calibration checks itself
    with pytest.raises(PrinterError, match="checked by the machine"):
        runner.setup_confirm("camera")
    markers = runner.calib_photo()
    assert markers and all(m["what"] for m in markers)
    cam = runner.setup_state()["camera"]
    assert cam["markers"] == markers  # the page lists them under the photo
    for m in markers[:3]:
        runner.motion.travel(*m["plate"])
        runner.calib_record(m["marker"])
    runner.calib_fit()
    assert runner.setup_state()["camera"]["fit"]["worst_mm"] < 1.0
    # 7 layout: bins found
    assert runner.scan_bins()["problems"] == []
    state = runner.setup_state()
    assert state["complete"], [s["title"] for s in state["steps"] if not s["done"]]
    assert runner.command("start").startswith("start")
    runner.wait(60)
    assert runner.state.status == "done" and not world.parts


def test_setup_moves_need_no_picker_but_sorting_does(machine):
    runner, _, printer = make(machine)
    runner.setup_check_printer()
    runner.setup_home()
    assert runner.setup_tool("hotend")["tool"] == "hotend"
    runner.jog(0, 0, 1)  # someone watches the Setup tab's moves: allowed
    with pytest.raises(PrinterError, match="Picker: OK"):
        runner.motion.preflight()  # sorting: not with the hotend
    with pytest.raises(ValueError, match="picker or hotend"):
        runner.setup_tool("nozzle")
    runner.setup_picker_ok()
    runner.motion.preflight()
    assert printer.log.count("TOOL_HOTEND") == printer.log.count("TOOL_PICKER") == 1


def test_the_picker_ok_needs_working_pins(machine):
    runner, _, _ = make(machine)
    runner.pins.ok, runner.pins.why = False, "gpiozero is missing"
    with pytest.raises(PrinterError, match="gpiozero is missing"):
        runner.setup_confirm("picker")
    assert not runner.setup.is_done("picker")


def test_no_model_sorting_refused_but_photos_work(machine):
    runner, _, _ = make(machine)
    runner.require_setup = False
    runner.vision.model_missing = lambda: "no model: upload one in the Models tab"
    runner.setup_home()
    assert runner.calib_photo()  # the codes are read without a model
    runner.command("start")
    runner.wait(10)
    assert runner.state.status == "error" and "no model" in runner.state.message


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
        keys = [s["key"] for s in state["steps"]]
        assert keys == ["printer", "homing", "machine", "picker", "tip", "camera", "layout"]
        assert post("/api/setup/home", {})["homed_now"] is True
        assert post("/api/setup/confirm", {"step": "homing"})["steps"][1]["done"] is True
        assert post("/api/setup/reset", {"step": "homing"})["steps"][1]["done"] is False
        assert post("/api/setup/confirm", {"step": "picker"})["tool"] == "picker"
        assert post("/api/command", {"command": "start"})["result"].startswith("finish the Setup")
    finally:
        srv.shutdown()


def test_the_tip_not_moved_between_codes_is_refused(machine):
    """Every 'Tip is here' at the same tip position gave a zero-size fit (error 0 mm, step
    ticked) and later 'singular matrix' in Find bins."""
    runner, _, _ = make(machine)
    runner.printer.gcode("G28")
    markers = runner.calib_photo()
    first, second = (
        markers[0],
        next(m for m in markers if abs(m["plate"][0] - markers[0]["plate"][0]) > 30),
    )
    runner.motion.travel(*first["plate"])
    runner.calib_record(first["marker"])
    with pytest.raises(PrinterError, match="jog it onto the centre"):
        runner.calib_record(second["marker"])  # the tip did not move
    # points like that from before (or edited) are refused by Fit as well
    runner.calib_pairs = [
        {"marker": 100 + i, "plate": [x, y], "bed": [50.0, 50.0]}
        for i, (x, y) in enumerate([(0, 0), (100, 0), (0, 100)])
    ]
    with pytest.raises(PrinterError, match="within 0 mm"):
        runner.calib_fit()
    assert not runner.setup.is_done("camera")


def test_a_saved_zero_size_fit_is_not_used(machine):
    """A bed_transform.json saved before the checks: Find bins says to redo step 6."""
    from sortering_pnp.io import write_json

    runner, _, _ = make(machine)
    runner.m.printer.driver = "moonraker"  # the message a real printer gets
    runner.setup.mark("camera")
    write_json(
        runner.transform_path,
        {"transform": {"A": [[0.0, 0.0], [0.0, 0.0]], "t": [50.0, 50.0]}, "pairs": []},
    )
    assert runner.calibration() is None
    with pytest.raises(PrinterError, match="do Setup step 6 again"):
        runner.transform()
    assert not runner.setup.is_done("camera")
