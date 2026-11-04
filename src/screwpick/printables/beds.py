"""Printer beds the baseplate can be clamped to: the grid stays the same, a flat border reaches to
the bed's edges so binder clips hold it (like a glass plate), and the whole thing is cut into
pieces that each fit on that printer.

Measure your own bed if it differs: the numbers here are the stock sizes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bed:
    key: str
    name: str
    size: tuple[float, float]  # the plate covers this, X x Y mm (front-left corner at 0, 0)
    printable: tuple[float, float]  # the largest piece that printer can print
    corner_cut_mm: float  # square notches at the corners (bed screws, levelling knobs)
    back_keep_mm: float  # the plate stops this far before the back edge (alignment pins)
    note: str


BEDS = {
    "ender3": Bed(
        "ender3",
        "Creality Ender 3 / Pro / V2",
        (235.0, 235.0),
        (220.0, 220.0),
        10.0,
        0.0,
        "235 x 235 mm aluminium bed (or glass on it); clamp the edges with binder clips",
    ),
    "mk3": Bed(
        "mk3",
        "Prusa i3 MK3 / MK3S (MK52 bed)",
        (254.0, 241.0),
        (250.0, 210.0),
        6.0,
        10.0,
        "on a spring steel sheet (254 x 241 mm), best a spare one: stick the pieces onto it with "
        "double-sided tape, and the sheet snaps onto the magnetic bed in the same place every "
        "time (its pins locate it). Binder clips also work, but their jaws under the sheet "
        "keep it from lying flat. The back stays 10 mm short of the two alignment pins",
    ),
}


def bed(key: str) -> Bed:
    if key not in BEDS:
        raise ValueError(f"unknown bed {key!r} (one of {', '.join(BEDS)})")
    return BEDS[key]
