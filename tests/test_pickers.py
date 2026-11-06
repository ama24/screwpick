"""The four picker types: where they aim, whether they turn, their Klipper file, the Setup tab."""

from __future__ import annotations

import numpy as np
import pytest

from sortering_pnp import pickers
from sortering_pnp.grip import Shape, axis_grip, head_grip
from sortering_pnp.io import read_json
from sortering_pnp.layout import Layout
from sortering_pnp.pins import PiPins
from sortering_pnp.printer import PrinterError
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D
from sortering_pnp.vision import parts_from_result

M4_SOCKET = Shape(d=4.0, head_r=3.5, head_k=4.0, socket=True)
KP = {"head_top": (0.0, 0.0), "bearing": (4.0, 0.0), "tip": (29.0, 0.0)}  # DIN912 M4x25


def test_four_types_in_order_and_the_old_names():
    assert [p.key for p in pickers.PICKERS] == [
        "servo_tweezers",
        "suction",
        "magnet",
        "pneumatic_tweezers",
    ]
    aims = {p.key: (p.aim, p.rotates) for p in pickers.PICKERS}
    assert aims == {
        "servo_tweezers": ("centre", True),
        "suction": ("head", False),
        "magnet": ("head", False),
        "pneumatic_tweezers": ("centre", True),
    }
    assert pickers.picker_type("vacuum").key == "suction"
    assert pickers.picker_type("tweezers").key == "servo_tweezers"
    with pytest.raises(ValueError, match="picker.mode"):
        pickers.picker_type("teleport")


def test_cup_and_magnet_aim_at_the_head_tweezers_at_the_centre_of_gravity():
    head = head_grip(KP, M4_SOCKET)
    assert head.method == "head" and head.point == pytest.approx((2.0, 0.0))
    shank = axis_grip(KP, M4_SOCKET)
    assert 4.0 < shank.point[0] < 29.0 and head.com == pytest.approx(shank.com)
    rod = Shape(d=4.0)  # headless: its centre either way
    assert head_grip(KP | {"bearing": (0.0, 0.0)}, rod).point == pytest.approx((14.5, 0.0))


def test_parts_from_result_aims_by_picker():
    obj = {
        "id": 1,
        "class": "DIN912_M4",
        "score": 0.9,
        "polygon": [[0, 0], [290, 0], [290, 70], [0, 70]],
        "position_mm": [14.5, 3.5],
        "keypoints": {"classical": {"head_top": [0, 35], "bearing": [40, 35], "tip": [290, 35]}},
    }

    def parts(aim):
        return parts_from_result(
            {"objects": [obj]},
            lambda px, h: np.c_[np.asarray(px, float) * 0.1, np.full(len(px), h)],
            {"DIN912_M4": 3.5}.get,
            {"DIN912_M4": M4_SOCKET}.get,
            aim,
        )[0]

    assert parts("head").grip_plate == pytest.approx((2.0, 3.5))
    assert parts("centre").grip_plate[0] > 5.5


def make(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=9)
    printer = SimPrinter(machine, world)
    return Runner(machine, printer, SimVision(world, lambda: lay, Transform2D.identity)), printer


def test_setup_choose_and_trim(machine):
    runner, printer = make(machine)
    st = runner.setup_picker_type("pneumatic_tweezers")
    assert st["picker"]["mode"] == "pneumatic_tweezers" and st["rotation"]
    assert machine.picker.kind.aim == "centre"
    assert st["picker"]["pins"] == {"ok": True, "message": "simulated pins"}
    runner.setup_picker_offset(12)
    runner.setup_rotate(90)
    assert printer.jaw_angle == pytest.approx(102.0)  # the trim is added
    st = runner.setup_picker_type("magnet")
    assert not st["rotation"]
    with pytest.raises(PrinterError, match="does not turn"):
        runner.setup_rotate(0)
    saved = read_json(machine.path(machine.paths.machine))
    assert saved["picker_mode"] == "magnet" and saved["rotate_offset_deg"] == 12.0
    again, _ = make(machine)  # a restart keeps the choice
    assert machine.picker.mode == "magnet"
    assert again.setup_state()["picker"]["offset_deg"] == 12.0


class FakeOutput:
    """gpiozero's DigitalOutputDevice, as far as PiPins uses it."""

    def __init__(self, pin):
        self.pin, self.value, self.closed = pin, 0, False

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0

    def close(self):
        self.closed = True


def fake_pwm(tmp_path):
    """/sys/class/pwm with one chip of two channels, already exported."""
    chip = tmp_path / "pwm" / "pwmchip0"
    for ch in ("pwm0", "pwm1"):
        (chip / ch).mkdir(parents=True)
        for f in ("period", "duty_cycle", "enable"):
            (chip / ch / f).write_text("0")
    (chip / "export").write_text("")
    return tmp_path / "pwm", chip


def test_pi_pins_switch_the_pump_on_and_off(machine, tmp_path):
    outs = []

    def output(pin):
        outs.append(FakeOutput(pin))
        return outs[-1]

    machine.picker.set_type("suction")
    waits = []
    pins = PiPins(machine, pwm_root=tmp_path / "none", output=output, sleep=waits.append)
    assert pins.status() == (True, "ready: GPIO12 (pump)")  # a pump needs no PWM
    pins.grab()
    assert outs[0].pin == 12 and outs[0].value == 1
    assert waits == [pytest.approx(0.4 + machine.picker.dwell_s)]  # the type's wait + dwell
    pins.release()
    assert outs[0].value == 0
    pins.grab()
    pins.off()  # stop, error, the program ends: the pump goes off
    assert outs[0].value == 0
    pins.close()
    assert outs[0].closed


def test_pi_pins_drive_servos_with_hardware_pwm(machine, tmp_path):
    root, chip = fake_pwm(tmp_path)
    machine.picker.set_type("servo_tweezers")
    pins = PiPins(machine, pwm_root=root, output=FakeOutput, sleep=lambda s: None)
    ok, why = pins.status()
    assert ok and "GPIO12 (servo)" in why and "GPIO13 (turning servo)" in why
    pins.grab()  # jaws to 20 degrees: 0.5 ms + 20/180 * 2 ms
    assert (chip / "pwm0" / "period").read_text() == "20000000"
    assert (chip / "pwm0" / "duty_cycle").read_text() == str(int(500_000 + 2_000_000 * 20 / 180))
    assert (chip / "pwm0" / "enable").read_text() == "1"
    machine.picker.release_angle = 100  # machine.yaml beats the type's 90
    pins.release()
    assert (chip / "pwm0" / "duty_cycle").read_text() == str(int(500_000 + 2_000_000 * 100 / 180))
    pins.turn(180)
    assert (chip / "pwm1" / "duty_cycle").read_text() == str(500_000)  # 180 = 0 (mod 180)
    pins.turn(90)
    assert (chip / "pwm1" / "duty_cycle").read_text() == "1500000"


def test_pi_pins_say_what_is_missing(machine, tmp_path):
    machine.picker.set_type("magnet")  # a servo
    pins = PiPins(machine, pwm_root=tmp_path / "none", output=FakeOutput)
    ok, why = pins.status()
    assert not ok and "install-pi.sh" in why and "reboot" in why
    with pytest.raises(PrinterError, match="hardware PWM"):
        pins.grab()

    def no_gpiozero(pin):
        raise ImportError("No module named 'gpiozero'")

    machine.picker.set_type("suction")
    ok, why = PiPins(machine, pwm_root=tmp_path, output=no_gpiozero).status()
    assert not ok and "python3-gpiozero" in why


def test_tweezers_on_a_stepper_need_no_turning_servo(machine, tmp_path):
    machine.picker.set_type("pneumatic_tweezers")  # pump + turning
    machine.picker.rotate_macro = "SORTERING_ROTATE"
    pins = PiPins(machine, pwm_root=tmp_path / "none", output=FakeOutput)
    ok, why = pins.status()
    assert ok and "SORTERING_ROTATE in Klipper" in why  # no PWM needed at all


def test_sortering_pnp_cfg_has_no_picker_in_klipper():
    from pathlib import Path

    klipper = Path(__file__).resolve().parents[1] / "klipper"
    main = (klipper / "sortering_pnp.cfg").read_text(encoding="utf-8")
    live = [ln for ln in main.splitlines() if ln and not ln.startswith("#")]
    assert not any(ln.startswith("[include") for ln in live)  # nothing to install
    assert not any("SORTERING_GRAB" in ln or "SORTERING_RELEASE" in ln for ln in live)
    assert "[mcu host]" not in main and not (klipper / "sortering_pnp_picker.cfg").exists()
