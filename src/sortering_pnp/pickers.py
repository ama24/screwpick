"""The four kinds of picker and how each is wired to the Raspberry Pi.

Everything hangs on two pins of the Pi, which sortering-pnp switches itself (pins.py): no
Klipper set-up for the picker. Every signal comes from the Pi, never from the printer's board,
so the wiring is the same on any printer (Ender 3, Prusa MK3, ...). Power comes straight from the
printer's 24 V supply (never its heater outputs): a 24 V pair to the tip, with the 5 V buck
converter for the servos there.

- GPIO12 (header pin 32), the "action" pin: jaw servo, magnet servo (hardware PWM 0), or the
  signal of a MOSFET module that switches a pump (on/off).
- GPIO13 (header pin 33), hardware PWM 1: the rotation servo (tweezers only, 0..180 degrees).
- GND (header pin 34), shared with the servos' 5 V supply and the MOSFET module.

Where a part is taken (`aim`): a suction cup or a magnet on the screw's head (flat and heavy,
it holds), tweezers at the centre of gravity, on the shank (grip.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


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
    grab_angle: float | None = None  # servo pickers: the angle that holds
    release_angle: float | None = None  # ... and the one that lets go
    grab_s: float = 0.3  # wait after grabbing (servo moves, pump pulls)
    release_s: float = 0.3  # wait after letting go (a cup without a valve lets go slowly)


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
        grab_angle=20,
        release_angle=90,
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
        grab_s=0.4,
        release_s=0.6,
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
        grab_angle=160,
        release_angle=20,
        release_s=0.4,
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
        grab_s=0.4,
        release_s=0.5,
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
