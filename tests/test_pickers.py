"""The four picker types: where they aim, whether they turn, their Klipper file, the Setup tab."""

from __future__ import annotations

import numpy as np
import pytest

from screwpick import pickers
from screwpick.grip import Shape, axis_grip, head_grip
from screwpick.io import read_json
from screwpick.layout import Layout
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D
from screwpick.vision import parts_from_result

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


@pytest.mark.parametrize("key", [p.key for p in pickers.PICKERS])
def test_klipper_file(key):
    text = pickers.klipper_config(key)
    t = pickers.BY_KEY[key]
    assert "[mcu host]" in text and "serial: /tmp/klipper_host_mcu" in text
    assert "[gcode_macro SCREWPICK_GRAB]" in text and "[gcode_macro SCREWPICK_RELEASE]" in text
    assert pickers.ACTION_PIN in text  # GPIO12 does the grabbing
    assert ("[gcode_macro SCREWPICK_ROTATE]" in text) == t.rotates
    assert (pickers.TURN_PIN in text) == t.rotates  # GPIO13 only turns
    assert ("[output_pin screwpick_pump]" in text) == t.pump
    sections = [ln for ln in text.splitlines() if ln.startswith("[")]
    assert len(sections) == len(set(sections))  # Klipper refuses a section twice


def make(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=9)
    printer = SimPrinter(machine, world)
    return Runner(machine, printer, SimVision(world, lambda: lay, Transform2D.identity)), printer


def test_setup_choose_install_and_trim(machine):
    runner, printer = make(machine)
    st = runner.setup_picker_type("pneumatic_tweezers")
    assert st["picker"]["mode"] == "pneumatic_tweezers" and st["rotation"]
    assert machine.picker.rotate == "SCREWPICK_ROTATE" and machine.picker.kind.aim == "centre"
    st = runner.setup_picker_install()
    assert st["picker"]["installed"] == "pneumatic_tweezers"
    assert printer.configs[pickers.CONFIG_NAME] == pickers.klipper_config("pneumatic_tweezers")
    assert "FIRMWARE_RESTART" in printer.log
    runner.setup_picker_offset(12)
    runner.setup_rotate(90)
    assert printer.jaw_angle == pytest.approx(102.0)  # the trim is added
    st = runner.setup_picker_type("magnet")
    assert not st["rotation"] and machine.picker.rotate is None
    assert st["picker"]["installed"] == "pneumatic_tweezers"  # the magnet's file is not in yet
    saved = read_json(machine.path(machine.paths.machine))
    assert saved["picker_mode"] == "magnet" and saved["rotate_offset_deg"] == 12.0
    again, _ = make(machine)  # a restart keeps the choice
    assert machine.picker.mode == "magnet"
    assert again.setup_state()["picker"]["offset_deg"] == 12.0


def test_screwpick_cfg_includes_the_picker_file_and_leaves_its_macros_to_it():
    from pathlib import Path

    klipper = Path(__file__).resolve().parents[1] / "klipper"
    main = (klipper / "screwpick.cfg").read_text(encoding="utf-8")
    assert f"[include {pickers.CONFIG_NAME}]" in main
    live = [ln for ln in main.splitlines() if ln.startswith("[")]
    assert (
        "[gcode_macro SCREWPICK_GRAB]" not in live and "[gcode_macro SCREWPICK_ROTATE]" not in live
    )
    placeholder = (klipper / pickers.CONFIG_NAME).read_text(encoding="utf-8")
    assert placeholder == pickers.PLACEHOLDER and "[mcu host]" not in placeholder
