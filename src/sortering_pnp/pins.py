"""The picker, switched straight from the Raspberry Pi's pins by sortering-pnp (not by Klipper).

sortering-pnp runs on the Pi next to Klipper, so it needs no second Klipper board for the
picker. The action pin (GPIO12, header pin 32) switches a pump through a MOSFET module (on/off)
or drives the jaw / magnet servo; the turn pin (GPIO13, pin 33) drives the tweezers' turning
servo. Servos get hardware PWM from /sys/class/pwm (the pwm-2chan overlay, which install-pi.sh
adds to config.txt); a pump is a plain output through gpiozero (part of Raspberry Pi OS).

Motion waits until Klipper has finished every move (M400) before it calls grab or release.
When the program stops, fails or ends, `off()` switches the pump off; a MOSFET module's
pull-down also keeps it off while nothing drives the pin.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .config import Machine
from .printer.base import PrinterError

log = logging.getLogger("sortering_pnp.pins")

PWM_ROOT = Path("/sys/class/pwm")
SERVO_PERIOD_NS = 20_000_000  # 50 Hz
SERVO_MIN_NS, SERVO_MAX_NS = 500_000, 2_500_000  # 0 and 180 degrees
PWM_CHANNEL = {12: 0, 13: 1, 18: 0, 19: 1}  # GPIO -> PWM channel (pwm-2chan overlay)
TURN_S = 0.35  # a turn of the tweezers takes this long


class Pins(Protocol):
    def grab(self) -> None: ...

    def release(self) -> None: ...

    def turn(self, angle_deg: float) -> None:
        """The turning servo to this angle (0..180, offset already added)."""

    def off(self) -> None:
        """Safe state: the pump off. Called on stop, errors and when the program ends."""

    def status(self) -> tuple[bool, str]:
        """Can the pins be used? (ok, what to do if not)"""

    def close(self) -> None: ...


class _Servo:
    """One hardware PWM channel in /sys/class/pwm/pwmchipN, as a hobby servo."""

    def __init__(self, chip: Path, channel: int) -> None:
        self.dir = chip / f"pwm{channel}"
        if not self.dir.is_dir():
            (chip / "export").write_text(str(channel))
            for _ in range(50):  # udev gives the gpio group access a moment later
                if (self.dir / "enable").exists():
                    break
                time.sleep(0.02)
        (self.dir / "period").write_text(str(SERVO_PERIOD_NS))
        self.enabled = False

    def angle(self, deg: float) -> None:
        a = min(max(float(deg), 0.0), 180.0)
        ns = SERVO_MIN_NS + (SERVO_MAX_NS - SERVO_MIN_NS) * a / 180.0
        (self.dir / "duty_cycle").write_text(str(int(ns)))
        if not self.enabled:
            (self.dir / "enable").write_text("1")
            self.enabled = True


def _gpiozero_output(pin: int) -> Any:
    from gpiozero import DigitalOutputDevice  # Raspberry Pi OS: python3-gpiozero

    return DigitalOutputDevice(pin, initial_value=False)


class PiPins:
    """The real pins. Opened on first use, so a PC (or a missing part) fails only there, with
    a message that says what to do."""

    def __init__(
        self,
        m: Machine,
        pwm_root: Path = PWM_ROOT,
        output: Callable[[int], Any] = _gpiozero_output,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.m = m
        self.pwm_root = pwm_root
        self._output = output
        self._sleep = sleep
        self._pump: Any = None
        self._servos: dict[int, _Servo] = {}
        self._mode: str | None = None  # the picker type the pins were opened for

    # ------------------------------------------------------------------ opening
    def _chip(self) -> Path:
        n = self.m.picker.pwm_chip
        if n is not None:
            chip = self.pwm_root / f"pwmchip{n}"
            if chip.is_dir():
                return chip
            raise PrinterError(f"no {chip}: is picker.pwm_chip right?")
        chips = sorted(self.pwm_root.glob("pwmchip*")) if self.pwm_root.is_dir() else []
        if not chips:
            raise PrinterError(
                "no hardware PWM for the servos: run install-pi.sh (it adds the pwm-2chan line "
                "to config.txt), then reboot once"
            )
        return chips[0]

    def _servo(self, gpio: int) -> _Servo:
        if gpio not in self._servos:
            if gpio not in PWM_CHANNEL:
                raise PrinterError(f"GPIO{gpio} has no hardware PWM: servos go on GPIO12/13")
            self._servos[gpio] = _Servo(self._chip(), PWM_CHANNEL[gpio])
        return self._servos[gpio]

    def _open(self) -> None:
        pk = self.m.picker
        if self._mode == pk.mode:
            return
        self.close()  # another type was chosen: let go of the old pins
        t = pk.kind
        try:
            if t.pump:
                self._pump = self._output(pk.action_pin)
            else:
                self._servo(pk.action_pin)
            if t.rotates and not pk.rotate_macro:  # a stepper turns through Klipper instead
                self._servo(pk.turn_pin)
        except PrinterError:
            raise
        except ImportError as exc:
            raise PrinterError(
                "gpiozero is missing (sudo apt install python3-gpiozero, or run install-pi.sh)"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - OSError, lgpio's "GPIO busy", gpiozero's own
            raise PrinterError(f"the Pi's pins cannot be used: {exc}") from exc
        self._mode = pk.mode

    def status(self) -> tuple[bool, str]:
        try:
            self._open()
        except PrinterError as exc:
            return False, str(exc)
        pk = self.m.picker
        t = pk.kind
        used = [f"GPIO{pk.action_pin} ({'pump' if t.pump else 'servo'})"]
        if t.rotates:
            used.append(
                f"turning: {pk.rotate_macro} in Klipper"
                if pk.rotate_macro
                else f"GPIO{pk.turn_pin} (turning servo)"
            )
        return True, "ready: " + ", ".join(used)

    def _ready(self) -> None:
        ok, why = self.status()
        if not ok:
            raise PrinterError(why)

    # ------------------------------------------------------------------ actions
    def _set(self, holding: bool) -> None:
        self._ready()
        pk = self.m.picker
        t = pk.kind
        if t.pump:
            if holding:
                self._pump.on()
            else:
                self._pump.off()
        else:
            angle = pk.grab_angle if holding else pk.release_angle
            if angle is None:
                angle = t.grab_angle if holding else t.release_angle
            self._servo(pk.action_pin).angle(float(angle or 0.0))
        self._sleep((t.grab_s if holding else t.release_s) + pk.dwell_s)

    def grab(self) -> None:
        self._set(True)

    def release(self) -> None:
        self._set(False)

    def turn(self, angle_deg: float) -> None:
        self._ready()
        self._servo(self.m.picker.turn_pin).angle(angle_deg % 180.0)
        self._sleep(TURN_S)

    def off(self) -> None:
        try:
            if self._pump is not None:
                self._pump.off()
        except Exception:  # noqa: BLE001 - the safe state must never raise
            log.exception("could not switch the pump off")

    def close(self) -> None:
        self.off()
        if self._pump is not None:
            try:
                self._pump.close()
            except Exception:  # noqa: BLE001
                log.exception("could not close the pump pin")
        self._pump = None
        self._servos = {}
        self._mode = None


def make_pins(m: Machine, printer: object) -> Pins:
    """The simulator's pins with the simulated printer, the Pi's pins otherwise."""
    from .printer.sim import SimPins, SimPrinter

    if isinstance(printer, SimPrinter):
        return SimPins(printer)
    return PiPins(m)
