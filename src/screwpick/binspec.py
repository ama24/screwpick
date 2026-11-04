"""Dimensions shared by the printable bins and the layout (where to find a bin's marker).

Gridfinity: 42 mm grid, 7 mm height units, bins 0.5 mm smaller than their cells. screwpick bins
have a 45-degree shelf along the back wall whose flat top carries the bin's marker, raised
MARKER_INK_MM above the rim so a single-extruder printer can switch to black filament for it.
"""

from __future__ import annotations

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
