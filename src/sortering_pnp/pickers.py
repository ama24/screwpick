"""The four kinds of picker, how each is wired to the Raspberry Pi and its Klipper config.

Everything hangs on two pins of the Pi, used by Klipper as a second micro-controller (its
"host" MCU, the Linux process). Every signal comes from the Pi, never from the printer's board,
so the wiring is the same on any printer (Ender 3, Prusa MK3, ...). Power comes straight from the
printer's 24 V supply (never its heater outputs): a 24 V pair to the tip, with the 5 V buck
converter for the servos there.

- GPIO12 (header pin 32), hardware PWM 0: the "action" pin. Jaw servo, magnet servo, or the
  signal of a MOSFET module that switches a pump.
- GPIO13 (header pin 33), hardware PWM 1: the rotation servo (tweezers only, 0..180 degrees).
- GND (header pin 34), shared with the servos' 5 V supply and the MOSFET module.

Where a part is taken (`aim`): a suction cup or a magnet on the screw's head (flat and heavy,
it holds), tweezers at the centre of gravity, on the shank (grip.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

ACTION_PIN = "host:pwmchip0/pwm0"  # GPIO12, header pin 32
TURN_PIN = "host:pwmchip0/pwm1"  # GPIO13, header pin 33
CONFIG_NAME = (
    "sortering_pnp_picker.cfg"  # in Klipper's config folder, included by sortering_pnp.cfg
)
# klipper-mcu.service makes this while the Pi runs as a Klipper board; without it Klipper
# cannot start with a picker file installed ("mcu 'host': Unable to connect")
HOST_MCU_SERIAL = "/tmp/klipper_host_mcu"


@dataclass(frozen=True)
class PickerType:
    key: str
    title: str
    summary: str
    aim: str  # "head" | "centre" (centre of gravity)
    rotates: bool  # a servo turns it 0..180 degrees, so the jaws close across the part
    holds: str  # "jaws" | "cup" | "magnet": where along the part's height it takes it
    pump: bool  # switched through a MOSFET module on the action pin
    servos: int  # hobby servos (5 V supply)
    parts: tuple[str, ...]  # what to buy


PICKERS: tuple[PickerType, ...] = (
    PickerType(
        "servo_tweezers",
        "Servo tweezers",
        "One servo opens and closes the tweezers, a second turns them (up to 180°) so the jaws "
        "close across the part.",
        "centre",
        True,
        "jaws",
        False,
        2,
        (
            "2 × micro servo (MG90S, metal gears)",
            "5 V buck converter, 3 A (e.g. LM2596 or XL4015), at the tip, fed by a 24 V pair "
            "from the printer's supply",
            "Dupont jumper wires",
        ),
    ),
    PickerType(
        "suction",
        "Suction cup",
        "A small vacuum pump holds the part on a cup. The pump is switched by a MOSFET module on "
        "the same pin the jaw servo would use. Nothing turns.",
        "head",
        False,
        "cup",
        True,
        0,
        (
            "24 V vacuum pump (or a 12 V one behind a 12 V buck converter)",
            "MOSFET module: a logic-level one (AOD4184 / D4184) switches fully at the Pi's "
            "3.3 V; the classic IRF520 module only half-opens at 3.3 V",
            "Flyback diode 1N4007 or 1N5819 across the pump (stripe to +)",
            "Silicone suction cup 6-10 mm, tubing",
        ),
    ),
    PickerType(
        "magnet",
        "Magnet",
        "A servo presses a magnet against an iron rod; the rod's tip touches the screw and holds "
        "it. The servo pulls the magnet away to let go. Nothing turns. Steel parts only.",
        "head",
        False,
        "magnet",
        False,
        1,
        (
            "1 × micro servo (MG90S)",
            "Neodymium magnet, iron rod (a nail or a 3-4 mm soft iron rod)",
            "5 V buck converter, 3 A, at the tip, fed by a 24 V pair from the printer's supply",
        ),
    ),
    PickerType(
        "pneumatic_tweezers",
        "Pneumatic tweezers",
        "A pump closes the tweezers (they spring open when it stops), a servo turns them (up to "
        "180°). The pump is switched by a MOSFET module.",
        "centre",
        True,
        "jaws",
        True,
        1,
        (
            "24 V pump (or 12 V behind a buck converter) and pneumatic tweezers",
            "MOSFET module, logic level (AOD4184 / D4184) as for the suction cup",
            "Flyback diode 1N4007 or 1N5819 across the pump",
            "1 × micro servo (MG90S) and a 5 V buck converter, 3 A, at the tip",
        ),
    ),
)
BY_KEY = {p.key: p for p in PICKERS}
# machine.yaml values from before the four types
LEGACY = {
    "vacuum": "suction",
    "gripper": "servo_tweezers",
    "tweezers": "servo_tweezers",
    "both": "servo_tweezers",
}
MODES = tuple(BY_KEY) + tuple(LEGACY)


def picker_type(mode: str) -> PickerType:
    """The type for a picker.mode (also the old names: vacuum, gripper, tweezers, both)."""
    key = LEGACY.get(mode, mode)
    if key not in BY_KEY:
        raise ValueError(f"picker.mode must be one of {MODES}, got {mode!r}")
    return BY_KEY[key]


def as_dict(p: PickerType) -> dict:
    return {**asdict(p), "parts": list(p.parts)}


_HOST_MCU = f"""\
# The Raspberry Pi as a second Klipper micro-controller, set up ONCE (or with KIAUH):
#   1. In /boot/firmware/config.txt add the line (hardware PWM on GPIO12 and GPIO13), reboot:
#        dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
#   2. cd ~/klipper && make clean && make menuconfig   (Architecture: Linux process)
#      make && sudo make flash
#      sudo cp ./scripts/klipper-mcu.service /etc/systemd/system/
#      sudo systemctl enable --now klipper-mcu.service
#   3. ls {HOST_MCU_SERIAL} must find it; only then install this file.
# Without it Klipper does not start ("mcu 'host': Unable to connect"): put the placeholder back
# (Setup tab, Picker: Take it out of Klipper) and FIRMWARE_RESTART.
# If printer.cfg already has a Linux-process MCU (e.g. [mcu rpi] for an accelerometer), delete
# the two lines below and write its name instead of "host:" in the pins.
[mcu host]
serial: {HOST_MCU_SERIAL}
"""

_SERVO = """\
[servo {name}]
pin: {pin}
maximum_servo_angle: 180
minimum_pulse_width: 0.0005
maximum_pulse_width: 0.0025
initial_angle: {initial}
"""

_PUMP = f"""\
[output_pin sortering_pnp_pump]
pin: {ACTION_PIN}               # GPIO12 (pin 32) -> SIG of the MOSFET module
pwm: True
hardware_pwm: True
cycle_time: 0.0001
value: 0
shutdown_value: 0
"""

_ROTATE = f"""\
{_SERVO.format(name="sortering_pnp_turn", pin=TURN_PIN + "   # GPIO13 (pin 33)", initial=0)}
[gcode_macro SORTERING_ROTATE]
description: Turn the tweezers so their jaws close along ANGLE (deg, 0 = printer X)
gcode:
  {{% set a = params.ANGLE|default(0)|float %}}
  SET_SERVO SERVO=sortering_pnp_turn ANGLE={{[[a, 0]|max, 180]|min}}
  G4 P350

[gcode_macro SORTERING_ROT_ZERO]
description: A servo knows its 0 by itself: trim it with the offset in the Setup tab
gcode:
  RESPOND MSG="sortering-pnp: servo rotation, 0 deg is fixed (trim: Setup tab, Picker)"
"""

_BODY = {
    "servo_tweezers": f"""\
{_SERVO.format(name="sortering_pnp_jaws", pin=ACTION_PIN + "   # GPIO12 (pin 32)", initial=90)}
[gcode_macro SORTERING_GRAB]
description: Picker: close the tweezers
variable_closed: 20             # servo angle with the jaws closed on a part
gcode:
  SET_SERVO SERVO=sortering_pnp_jaws ANGLE={{printer["gcode_macro SORTERING_GRAB"].closed}}
  G4 P300

[gcode_macro SORTERING_RELEASE]
description: Picker: open the tweezers
variable_open: 90
gcode:
  SET_SERVO SERVO=sortering_pnp_jaws ANGLE={{printer["gcode_macro SORTERING_RELEASE"].open}}
  G4 P300

{_ROTATE}""",
    "suction": f"""\
{_PUMP}
[gcode_macro SORTERING_GRAB]
description: Picker: pump on, the cup holds
gcode:
  SET_PIN PIN=sortering_pnp_pump VALUE=1
  G4 P400

[gcode_macro SORTERING_RELEASE]
description: Picker: pump off, the cup lets go (slowly without a release valve)
gcode:
  SET_PIN PIN=sortering_pnp_pump VALUE=0
  G4 P600
""",
    "magnet": f"""\
{_SERVO.format(name="sortering_pnp_magnet", pin=ACTION_PIN + "   # GPIO12 (pin 32)", initial=20)}
[gcode_macro SORTERING_GRAB]
description: Picker: magnet against the rod, the rod holds
variable_on: 160                # servo angle with the magnet pressed against the rod
gcode:
  SET_SERVO SERVO=sortering_pnp_magnet ANGLE={{printer["gcode_macro SORTERING_GRAB"].on}}
  G4 P300

[gcode_macro SORTERING_RELEASE]
description: Picker: magnet away from the rod, the part drops
variable_off: 20
gcode:
  SET_SERVO SERVO=sortering_pnp_magnet ANGLE={{printer["gcode_macro SORTERING_RELEASE"].off}}
  G4 P400
""",
    "pneumatic_tweezers": f"""\
{_PUMP}
[gcode_macro SORTERING_GRAB]
description: Picker: pump on, the tweezers close
gcode:
  SET_PIN PIN=sortering_pnp_pump VALUE=1
  G4 P400

[gcode_macro SORTERING_RELEASE]
description: Picker: pump off, the tweezers spring open
gcode:
  SET_PIN PIN=sortering_pnp_pump VALUE=0
  G4 P500

{_ROTATE}""",
}


def klipper_config(mode: str) -> str:
    """sortering_pnp_picker.cfg for this picker: the Pi as an MCU, its pins and the macros."""
    p = picker_type(mode)
    return (
        f"# sortering_pnp picker: {p.title}. Written by sortering_pnp (Setup tab, Picker); choose another\n"
        f"# type there to replace this file. Aims at the {'head' if p.aim == 'head' else 'centre of gravity'}"
        f"{', turns 0-180 deg' if p.rotates else ''}.\n#\n" + _HOST_MCU + "\n" + _BODY[p.key]
    )


PLACEHOLDER = f"""\
# sortering_pnp picker: not chosen yet. Choose the picker in sortering_pnp's Setup tab (Picker) and press
# "Install in Klipper": it replaces this file ({CONFIG_NAME}).
[gcode_macro SORTERING_GRAB]
gcode:
  RESPOND TYPE=error MSG="sortering-pnp: choose the picker in the Setup tab first"

[gcode_macro SORTERING_RELEASE]
gcode:
  RESPOND MSG="sortering-pnp: no picker chosen yet"
"""
