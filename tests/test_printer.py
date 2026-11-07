from __future__ import annotations

import copy
import http.server
import json
import threading
import urllib.parse

import pytest

from sortering_pnp.layout import Layout
from sortering_pnp.printer import Motion, PrinterError
from sortering_pnp.printer.base import PrinterStatus
from sortering_pnp.printer.moonraker import Moonraker, parse_remote_call
from sortering_pnp.printer.sim import SimPins, SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


class FakePrinter:
    def __init__(self, status: PrinterStatus) -> None:
        self.st = status
        self.sent: list[str] = []

    def gcode(self, script: str) -> None:
        self.sent.append(script)
        if script.strip() == "G28":
            self.st.homed = "xyz"

    def status(self, tool_macro=None) -> PrinterStatus:
        return self.st

    def emergency_stop(self) -> None:
        self.sent.append("ESTOP")

    def close(self) -> None:
        pass


def ready(**kw) -> PrinterStatus:
    d = {
        "state": "ready",
        "homed": "xyz",
        "position": [10.0, 20.0, 30.0],
        "hotend_c": 25.0,
        "tool": "picker",
    }
    d.update(kw)
    return PrinterStatus(**d)


@pytest.mark.parametrize(
    ("kw", "text"),
    [
        ({"state": "shutdown"}, "not ready"),
        ({"printing": True}, "printing"),
        ({"hotend_c": 180.0}, "hotend is 180"),
        ({"tool": "hotend"}, "TOOL_PICKER"),
        ({"tool": None}, "include sortering_pnp.cfg"),
    ],
)
def test_preflight_refuses(machine, kw, text):
    with pytest.raises(PrinterError, match=text):
        Motion(FakePrinter(ready(**kw)), machine).preflight()


def test_preflight_homes_when_needed(machine):
    fp = FakePrinter(ready(homed="xy"))
    Motion(fp, machine).preflight()
    assert fp.sent == ["G28"]
    machine.printer.auto_home = False
    with pytest.raises(PrinterError, match="not homed"):
        Motion(FakePrinter(ready(homed="")), machine).preflight()


def test_moves_apply_tool_offset_and_limits(machine):
    machine.tool.offset = [5.0, -2.0, 1.0]
    fp = FakePrinter(ready())
    mo = Motion(fp, machine)
    assert mo.current_tip() == [15.0, 18.0, 31.0]
    mo.move(x=50.0, y=60.0)
    assert fp.sent[-1] == "G90\nG1 X45.000 Y62.000 F6000"
    mo.move(z=11.0)
    assert fp.sent[-1] == "G90\nG1 Z10.000 F1200"
    with pytest.raises(PrinterError, match="limits"):
        mo.move(x=300.0)
    with pytest.raises(PrinterError, match="Z"):
        mo.move(z=0.5)  # toolhead Z -0.5


class RecordingPins:
    """The picker's pins, writing what they do into the printer's list (to check the order)."""

    def __init__(self, sent: list[str]) -> None:
        self.sent = sent

    def grab(self) -> None:
        self.sent.append("PINS grab")

    def release(self) -> None:
        self.sent.append("PINS release")

    def turn(self, angle_deg: float) -> None:
        self.sent.append(f"PINS turn {angle_deg:.1f}")

    def off(self) -> None:
        self.sent.append("PINS off")

    def status(self):
        return True, "recording"

    def close(self) -> None:
        pass


def test_pick_and_drop_sequence(machine):
    machine.picker.mode = "gripper"  # the old name of the servo tweezers: they turn
    fp = FakePrinter(ready(position=[0.0, 0.0, 50.0]))
    mo = Motion(fp, machine, pins=RecordingPins(fp.sent))
    mo.pick(100.0, 90.0, 3.0, angle_deg=200.0)
    text = "\n".join(fp.sent)
    assert "PINS turn 20.0" in text
    assert "G1 Z11.000" in text and "G1 Z3.000 F300" in text
    # the pins are not in Klipper's queue: every move has finished (M400) before the grab
    grab = fp.sent.index("PINS grab")
    assert fp.sent[grab - 1] == "M400" and "G1 Z3.000 F300" in fp.sent[grab - 2]
    assert fp.sent[-1].endswith("Z50.000 F1200")
    fp.sent.clear()
    machine.picker.lower_to_drop = True
    mo.drop(20.0, 20.0, rim_z=21.0)
    assert fp.sent.index("PINS release") == fp.sent.index("M400") + 1
    assert "G1 Z31.000" in "\n".join(fp.sent)


def test_tweezers_on_a_stepper_turn_through_klipper(machine):
    machine.picker.mode = "servo_tweezers"
    machine.picker.rotate_macro = "SORTERING_ROTATE"
    fp = FakePrinter(ready(position=[0.0, 0.0, 50.0]))
    Motion(fp, machine, pins=RecordingPins(fp.sent)).pick(100.0, 90.0, 3.0, angle_deg=200.0)
    assert "SORTERING_ROTATE ANGLE=20.0" in fp.sent and not any("PINS turn" in s for s in fp.sent)


def test_shake_returns_to_place(machine):
    fp = FakePrinter(ready(position=[0.0, 200.0, 60.0]))
    Motion(fp, machine).shake()
    lines = fp.sent[-1].splitlines()
    assert lines[1] == "G91" and lines[-2] == "G90"
    total = sum(float(line.split()[1][1:]) for line in lines if line.startswith("G1 Y"))
    assert total == pytest.approx(0.0)


def test_sim_printer_picks_up_parts(machine):
    from sortering_pnp.layout import Layout

    world = SimWorld()
    lay = Layout.default()
    world.populate(lay, n=1)
    sp = world.parts[0]
    pr = SimPrinter(machine, world)
    pins = SimPins(pr)
    pr.gcode("G28")
    pr.gcode(f"G1 X{sp.x} Y{sp.y} Z{lay.plate_thickness_mm + sp.top - 0.5}")
    pins.grab()
    assert world.held is sp and not world.parts
    pr.gcode("G1 X20 Y20 Z40")
    pins.release()
    assert world.dropped == [(sp, (20.0, 20.0))]
    with pytest.raises(PrinterError):
        pr.gcode("M104 S200")


class _Moonraker(http.server.BaseHTTPRequestHandler):
    scripts: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path == "/printer/info":
            self._send({"result": {"state": "ready", "state_message": "Printer is ready"}})
        elif url.path == "/printer/objects/query":
            q = urllib.parse.parse_qs(url.query)
            assert "gcode_macro _SORTERING_TOOL" in q
            self._send(
                {
                    "result": {
                        "status": {
                            "toolhead": {"position": [1, 2, 3, 0], "homed_axes": "xyz"},
                            "extruder": {"temperature": 23.4},
                            "print_stats": {"state": "standby"},
                            "gcode_macro _SORTERING_TOOL": {"tool": "picker"},
                        }
                    }
                }
            )

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        body = json.loads(raw or b"{}")
        if self.path == "/printer/gcode/script":
            if body["script"] == "BAD":
                self._send({"error": {"message": 'Unknown command:"BAD"'}}, 400)
                return
            self.scripts.append(body["script"])
            self._send({"result": "ok"})


def test_moonraker_client():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Moonraker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mr = Moonraker(f"http://127.0.0.1:{srv.server_address[1]}", timeout_s=5)
        st = mr.status("_SORTERING_TOOL")
        assert st.ready and st.homed == "xyz" and st.position == [1, 2, 3]
        assert st.hotend_c == 23.4 and st.tool == "picker" and not st.printing
        mr.gcode("G28")
        assert _Moonraker.scripts == ["G28"]
        with pytest.raises(PrinterError, match="Unknown command"):
            mr.gcode("BAD")
    finally:
        srv.shutdown()
    with pytest.raises(PrinterError, match="cannot reach"):
        Moonraker("http://127.0.0.1:9", timeout_s=1).info()


def test_parse_remote_call():
    assert parse_remote_call(
        {"method": "sortering_pnp", "params": {"command": "start", "strategy": "sort"}}
    ) == {
        "command": "start",
        "strategy": "sort",
    }
    assert parse_remote_call({"method": "sortering_pnp", "params": [{"CMD": "stop"}]}) == {
        "cmd": "stop"
    }
    assert parse_remote_call({"method": "notify_status_update", "params": []}) is None


# letting go over a bin: travel height by default, lowered when picker.lower_to_drop is on


def _drop(machine, rim_z: float) -> list[str]:
    fp = FakePrinter(ready(position=[0.0, 0.0, machine.printer.travel_z]))
    Motion(fp, machine, pins=RecordingPins(fp.sent)).drop(20.0, 20.0, rim_z=rim_z)
    return fp.sent[: fp.sent.index("PINS release")]


def test_by_default_the_part_is_let_go_at_the_travel_height(machine):
    assert machine.picker.lower_to_drop is False
    before = _drop(machine, rim_z=21.0)
    assert not any(" Z" in s for s in before)  # no Z move before the release


def test_never_below_rim_plus_drop_even_with_a_low_travel_height(machine):
    machine.printer.travel_z = 30.0  # below a 6u bin's rim + drop_mm
    assert "G1 Z52.000" in "\n".join(_drop(machine, rim_z=42.0))


def test_lowering_is_the_old_behaviour(machine):
    machine.picker.lower_to_drop = True
    assert "G1 Z31.000" in "\n".join(_drop(machine, rim_z=21.0))


def test_the_tick_box_is_kept_over_a_restart(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    runner = Runner(machine, SimPrinter(machine, world), vision)
    st = runner.setup_picker_drop(True)
    assert st["picker"]["lower_to_drop"] is True and machine.picker.lower_to_drop
    fresh = copy.deepcopy(machine)  # after a restart
    fresh.picker.lower_to_drop = False
    again = Runner(fresh, SimPrinter(fresh, SimWorld()), vision)
    assert again.m.picker.lower_to_drop is True
