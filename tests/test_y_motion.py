"""Gentle bed moves (printer.y_motion): lowered acceleration and an S-shaped speed for Y moves."""

from __future__ import annotations

import re

import pytest

from sortering_pnp import config as config_mod
from sortering_pnp.printer import Motion
from sortering_pnp.printer.base import PrinterStatus
from sortering_pnp.printer.sim import SimPrinter, SimWorld
from sortering_pnp.printer.smooth import s_curve


class FakePrinter:
    def __init__(self, position: list[float], max_accel: float | None = 3000.0) -> None:
        self.st = PrinterStatus(
            state="ready", homed="xyz", position=position, tool="picker", max_accel=max_accel
        )
        self.sent: list[str] = []

    def gcode(self, script: str) -> None:
        self.sent.append(script)

    def status(self, tool_macro=None) -> PrinterStatus:
        return self.st

    def emergency_stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def g1(line: str) -> dict[str, float]:
    return {a: float(v) for a, v in re.findall(r"([XYZF])(-?\d+(?:\.\d+)?)", line)}


def moves(script: str) -> list[dict[str, float]]:
    return [g1(ln) for ln in script.splitlines() if ln.startswith("G1 ")]


def test_y_travel_is_an_s_curve_with_lowered_accel(machine):
    fp = FakePrinter([10.0, 0.0, 50.0])
    Motion(fp, machine).move(y=200.0)
    lines = fp.sent[-1].splitlines()
    assert lines[0] == "G90"
    assert lines[1] == "SET_VELOCITY_LIMIT ACCEL=1000"
    assert lines[-1] == "SET_VELOCITY_LIMIT ACCEL=3000"  # set back for X and Z
    ms = moves(fp.sent[-1])
    # 6000 mm/min at 1000 mm/s^2: 7.5 mm ramps, 1 mm steps at least: 7 up, cruise, 7 down
    assert len(ms) == 15
    assert lines[-2] == "G1 Y200.000 F87"  # the exact end point
    assert all(set(m) == {"Y", "F"} for m in ms)  # X and Z do not move
    ys = [m["Y"] for m in ms]
    assert ys == sorted(ys) and ys[-1] == 200.0
    fs = [m["F"] for m in ms]
    peak = fs.index(max(fs))
    assert fs[: peak + 1] == sorted(fs[: peak + 1])  # rises ...
    assert fs[peak:] == sorted(fs[peak:], reverse=True)  # ... and falls
    assert max(fs) == machine.printer.feed_xy
    # S-shaped: the speed steps are small at both ends of the ramp, largest in its middle
    ramp = fs[:7]
    jumps = [b - a for a, b in zip([0.0, *ramp], ramp, strict=False)]
    assert jumps[0] < jumps[3] and jumps[-1] < jumps[3]


def test_s_curve_peak_accel_and_distances():
    steps = s_curve(200.0, 6000.0, 1000.0, 8, 1.0)
    assert steps[-1][0] == pytest.approx(200.0)
    ends = [d for d, _ in steps]
    assert ends == sorted(ends)
    # accelerating from one step's speed to the next never needs more than the peak accel
    prev_v, prev_d = 0.0, 0.0
    for d, f in steps[:8]:
        v = f / 60.0
        assert (v * v - prev_v * prev_v) / (2 * (d - prev_d)) <= 1000.0 * 1.6
        prev_v, prev_d = v, d


def test_short_moves_get_few_or_no_steps(machine):
    assert len(s_curve(5.0, 6000.0, 1000.0, 8, 1.0)) < 8
    assert s_curve(1.0, 6000.0, 1000.0, 8, 1.0) == [(1.0, 6000.0)]
    fp = FakePrinter([10.0, 100.0, 50.0])
    Motion(fp, machine).move(y=101.0)  # a 1 mm nudge: one G1, still at the gentle accel
    assert fp.sent[-1].splitlines() == [
        "G90",
        "SET_VELOCITY_LIMIT ACCEL=1000",
        "G1 Y101.000 F6000",
        "SET_VELOCITY_LIMIT ACCEL=3000",
    ]


def test_xy_move_stays_on_a_straight_line(machine):
    machine.tool.offset = [5.0, -2.0, 0.0]
    fp = FakePrinter([0.0, 0.0, 50.0])
    mo = Motion(fp, machine)
    mo.move(x=105.0, y=148.0)  # tip; toolhead (100, 150): from (0, 0)
    ms = moves(fp.sent[-1])
    assert len(ms) > 3
    for m in ms[:-1]:
        assert m["X"] / 100.0 == pytest.approx(m["Y"] / 150.0, abs=1e-4)
    assert (ms[-1]["X"], ms[-1]["Y"]) == (100.0, 150.0)
    # the bed gets 1000 mm/s^2: the path, which is longer than its Y part, a bit more
    accel = float(fp.sent[-1].splitlines()[1].split("=")[1])
    assert accel == pytest.approx(1000.0 * (100**2 + 150**2) ** 0.5 / 150.0, abs=1)


def test_steps_stay_inside_the_limits(machine):
    machine.printer.limits_y = [0.0, 220.0]
    fp = FakePrinter([0.0, 220.0, 50.0])
    Motion(fp, machine).move(x=220.0, y=0.0)
    for m in moves(fp.sent[-1]):
        assert 0.0 <= m["X"] <= 220.0 and 0.0 <= m["Y"] <= 220.0


def test_x_and_z_moves_are_unchanged(machine):
    fp = FakePrinter([10.0, 20.0, 50.0])
    mo = Motion(fp, machine)
    mo.move(x=150.0)
    assert fp.sent[-1] == "G90\nG1 X150.000 F6000"
    mo.move(x=150.0, y=20.0)  # Y given but not changing
    assert fp.sent[-1] == "G90\nG1 X150.000 Y20.000 F6000"
    mo.move(z=10.0)
    assert fp.sent[-1] == "G90\nG1 Z10.000 F1200"


def test_disabled_is_the_old_single_move(machine):
    machine.printer.y_motion.enabled = False
    fp = FakePrinter([10.0, 0.0, 50.0])
    Motion(fp, machine).move(y=200.0)
    assert fp.sent[-1] == "G90\nG1 Y200.000 F6000"


def test_trapezoid_profile_and_max_feed(machine):
    ym = machine.printer.y_motion
    ym.profile = "trapezoid"
    ym.max_feed = 3000.0
    fp = FakePrinter([10.0, 0.0, 50.0])
    Motion(fp, machine).move(y=200.0)
    assert fp.sent[-1].splitlines() == [
        "G90",
        "SET_VELOCITY_LIMIT ACCEL=1000",
        "G1 Y200.000 F3000",
        "SET_VELOCITY_LIMIT ACCEL=3000",
    ]


def test_printer_accel_is_the_one_restored(machine):
    machine.printer.accel = 2000.0
    fp = FakePrinter([10.0, 0.0, 50.0], max_accel=None)
    Motion(fp, machine).move(y=50.0)
    assert fp.sent[-1].splitlines()[-1] == "SET_VELOCITY_LIMIT ACCEL=2000"
    machine.printer.accel = None
    fp = FakePrinter([10.0, 0.0, 50.0], max_accel=None)  # unknown: left lowered (safe side)
    Motion(fp, machine).move(y=50.0)
    lines = fp.sent[-1].splitlines()
    assert lines[1] == "SET_VELOCITY_LIMIT ACCEL=1000" and "ACCEL" not in lines[-1]


def test_gentle_accel_never_above_the_normal_one(machine):
    machine.printer.y_motion.accel = 5000.0
    fp = FakePrinter([10.0, 0.0, 50.0])
    Motion(fp, machine).move(y=100.0)
    assert "SET_VELOCITY_LIMIT" not in fp.sent[-1]


def test_shake_keeps_its_sharp_moves(machine):
    fp = FakePrinter([0.0, 200.0, 60.0])
    Motion(fp, machine).shake()
    shake = fp.sent[-1]
    assert "SET_VELOCITY_LIMIT" not in shake
    assert f"G1 Y4.00 F{machine.run.shake.feed:.0f}" in shake


def test_sim_follows_and_ends_exactly(machine):
    pr = SimPrinter(machine, SimWorld())
    pr.gcode("G28")
    mo = Motion(pr, machine)
    mo.move(z=50.0)
    mo.move(x=37.5, y=181.25)
    assert pr.pos[:2] == [37.5, 181.25]
    assert pr.accel == 3000.0
    assert "SET_VELOCITY_LIMIT ACCEL=1021" in pr.log  # 1000 for the bed: Y is 98% of the path


def test_config_checks_the_profile(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("printer:\n  y_motion:\n    profile: jerk\n", encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="profile"):
        config_mod.load(p)
    p.write_text(
        "printer:\n  y_motion:\n    accel: 600\n    profile: trapezoid\n", encoding="utf-8"
    )
    assert config_mod.load(p).printer.y_motion.accel == 600
