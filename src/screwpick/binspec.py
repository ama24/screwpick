"""Dimensions shared by the printable bins and the layout (where to find a bin's marker).

Gridfinity: 42 mm grid, 7 mm height units, bins 0.5 mm smaller than their cells. screwpick bins
have a 45-degree shelf along the back wall whose flat top carries the bin's marker, raised
MARKER_INK_MM above the rim so a single-extruder printer can switch to black filament for it.
"""

from __future__ import annotations

from dataclasses import dataclass

PITCH_MM = 42.0
HEIGHT_UNIT_MM = 7.0
GAP_MM = 0.5  # bin outer size = units * pitch - gap
BIN_RADIUS_MM = 3.75
BASE_HEIGHT_MM = 4.75  # the Gridfinity foot
WALL_MM = 1.2
FLOOR_MM = 1.25  # floor thickness above the foot
SHELF_DEPTH_MM = 14.0  # marker shelf, measured from the inside of the back wall
MARKER_CELLS = 6  # 4x4 ArUco bits plus the black border
MARKER_CELL_MM = 1.6
MARKER_INK_MM = 0.6  # height of the black marker cells above the rim
MIN_HEIGHT_U = 3  # the shelf needs the room


BASEPLATE_MM = 4.65  # standard Gridfinity baseplate
BASEPLATE_SLIM_MM = 2.5  # slim: only the straight part of the socket, which locates the feet


@dataclass(frozen=True)
class BinSize:
    w: int  # cells along X
    d: int  # cells along Y (the shelf with the marker is on a d side)
    height_u: int
    markers: range  # every bin of this size carries one of these ids: its size is known
    use: str

    @property
    def name(self) -> str:
        return f"{self.w}x{self.d}x{self.height_u}u"


# Fixed marker ranges per size: the camera knows a new bin's size (and its rim height, for the
# drop) from its marker alone. 220-249 are free for other sizes, set in the layout by hand.
SIZES = (
    BinSize(1, 1, 3, range(100, 140), "sorting: one part number on the printer bed"),
    BinSize(1, 2, 3, range(140, 160), "sorting: long screws, threaded rod, brackets"),
    BinSize(1, 1, 6, range(160, 180), "storage: small parts"),
    BinSize(1, 2, 6, range(180, 200), "storage: long parts"),
    BinSize(2, 2, 6, range(200, 220), "storage: large quantities"),
)


def size_for_marker(marker: int) -> BinSize | None:
    return next((s for s in SIZES if marker in s.markers), None)


def size_named(name: str) -> BinSize:
    for s in SIZES:
        if s.name == name:
            return s
    raise ValueError(f"unknown bin size {name!r} (one of {', '.join(s.name for s in SIZES)})")


def outer_mm(units: int) -> float:
    return units * PITCH_MM - GAP_MM


def rim_height_mm(height_u: int) -> float:
    """Height of the bin's rim (and the shelf top) above its bottom."""
    return height_u * HEIGHT_UNIT_MM


def marker_offset_mm(depth_units: int) -> tuple[float, float]:
    """Marker centre relative to the bin centre, in the bin frame (+Y = back, shelf side)."""
    return 0.0, outer_mm(depth_units) / 2 - WALL_MM - SHELF_DEPTH_MM / 2


def marker_size_mm() -> float:
    return MARKER_CELLS * MARKER_CELL_MM
