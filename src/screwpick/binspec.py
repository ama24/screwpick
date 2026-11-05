"""Dimensions shared by the printable bins and the layout (where to find a bin's marker).

Gridfinity: 42 mm grid, 7 mm height units, bins 0.5 mm smaller than their cells. Every bin
carries its marker flush with the rim, raised MARKER_INK_MM so a single-extruder printer can
switch to black filament for it, in one of two styles:

- "shelf": a 45-degree shelf along the whole back wall (ids 100-249);
- "corner": a square post, straight down to the floor, in the back-right corner (ids 50-99).
  Nothing overhangs, so no part can hide under it: made for the plate, where parts are thrown.
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
POST_MM = 14.0  # corner post, square, measured from the inside of the walls
MIN_HEIGHT_U = 3  # the shelf needs the room
STYLES = ("shelf", "corner")


BASEPLATE_MM = 4.65  # standard Gridfinity baseplate
BASEPLATE_SLIM_MM = 2.5  # slim: only the straight part of the socket, which locates the feet


@dataclass(frozen=True)
class BinSize:
    w: int  # cells along X
    d: int  # cells along Y (the shelf with the marker is on a d side)
    height_u: int
    markers: range  # every bin of this size carries one of these ids: its size is known
    use: str
    in_catalogue: int | None = None  # how many go into prints/ (None: the whole range)
    style: str = "shelf"  # where the marker sits: shelf | corner

    @property
    def name(self) -> str:
        base = f"{self.w}x{self.d}x{self.height_u}u"
        return base if self.style == "shelf" else f"{base}-{self.style}"


# Fixed marker ranges per size: the camera knows a new bin's size (and its rim height, for the
# drop) from its marker alone, and where on the bin the marker sits (shelf or corner post).
# 240-249 are free for other sizes, set in the layout by hand (shelf style). Trays (3x3, 4x4)
# are the plate: the bin the parts are thrown into. 0-49 belong to the calibration board.
SIZES = (
    BinSize(1, 1, 3, range(100, 140), "sorting: one part number on the printer bed"),
    BinSize(1, 2, 3, range(140, 160), "sorting: long screws, threaded rod, brackets"),
    BinSize(1, 1, 6, range(160, 180), "storage: small parts"),
    BinSize(1, 2, 6, range(180, 200), "storage: long parts"),
    BinSize(2, 2, 6, range(200, 220), "storage: large quantities"),
    BinSize(3, 3, 3, range(220, 230), "plate: the tray parts are thrown into", 2),
    BinSize(4, 4, 3, range(230, 240), "plate: a large tray for big jobs", 2),
    # the same sizes with the marker on a corner post
    BinSize(1, 1, 3, range(50, 66), "sorting, corner code", style="corner"),
    BinSize(1, 2, 3, range(66, 74), "sorting long parts, corner code", style="corner"),
    BinSize(1, 1, 6, range(74, 82), "storage, corner code", style="corner"),
    BinSize(1, 2, 6, range(82, 86), "storage long parts, corner code", style="corner"),
    BinSize(2, 2, 6, range(86, 90), "storage large quantities, corner code", style="corner"),
    BinSize(3, 3, 3, range(90, 95), "plate: nothing overhangs, no part gets trapped", 2, "corner"),
    BinSize(4, 4, 3, range(95, 100), "plate for big jobs, nothing overhangs", 2, "corner"),
)
CORNER_IDS = range(50, 100)
MARKER_IDS = range(50, 250)  # DICT_4X4_250; ids below 50 are the calibration board's


def style_for_marker(marker: int) -> str:
    """Where a bin's marker sits, from its id: corner post (50-99) or shelf (100-249)."""
    return "corner" if marker in CORNER_IDS else "shelf"


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


def marker_offset_mm(
    depth_units: int, width_units: int = 1, style: str = "shelf"
) -> tuple[float, float]:
    """Marker centre relative to the bin centre, in the bin frame (+Y = back, the marker side;
    +X = right): the middle of the back shelf, or the top of the back-right corner post."""
    if style == "corner":
        return (
            outer_mm(width_units) / 2 - WALL_MM - POST_MM / 2,
            outer_mm(depth_units) / 2 - WALL_MM - POST_MM / 2,
        )
    if style != "shelf":
        raise ValueError(f"unknown bin style {style!r} (one of {', '.join(STYLES)})")
    return 0.0, outer_mm(depth_units) / 2 - WALL_MM - SHELF_DEPTH_MM / 2


def marker_offset_for(marker: int, own_w: int, own_d: int) -> tuple[float, float]:
    """marker_offset_mm for the bin carrying `marker`, (own_w, own_d) in its own frame."""
    return marker_offset_mm(own_d, own_w, style_for_marker(marker))


def marker_size_mm() -> float:
    return MARKER_CELLS * MARKER_CELL_MM
