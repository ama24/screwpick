"""How full a bin is: the volume of the items that went in, against the room inside the bin.

Generic, for any item the model segments (screws today, anything tomorrow): an item's volume is
estimated from what the camera measured, its outline area on the plate times its height, times
a shape factor (a lying screw fills about half of that box: round shank, the head only at one
end). Loose items never fill a bin solid: `packing` is the share of the room they really
occupy. Both are settings (run.fill), worth tuning once a bin of known contents is full.

A bin's room comes from its Gridfinity size: the inside from the floor to the rim, without the
marker shelf or corner post. So a 1x2 bin holds twice a 1x1, and a 6-unit bin a little over
twice a 3-unit one (the floor is the same).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import binspec


@dataclass
class Fill:
    shape: float = 0.5  # item volume = outline area x height x this
    packing: float = 0.3  # loose items fill this share of a bin's room
    full_at: float = 0.9  # a bin this full (estimated) asks to be emptied
    default_mm3: float = 300.0  # an item without an outline (nothing measured)


def polygon_area(poly: np.ndarray) -> float:
    if len(poly) < 3:
        return 0.0
    x, y = poly[:, 0], poly[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def item_volume_mm3(part, fill: Fill) -> float:  # noqa: ANN001 - parts.Part
    """Estimated volume of one item from its segmentation outline and its height."""
    area = polygon_area(np.asarray(part.polygon, dtype=float))
    if area <= 0 or not part.top_mm or part.top_mm <= 0:
        return fill.default_mm3
    return area * float(part.top_mm) * fill.shape


def bin_room_mm3(w: int, d: int, height_u: int, marker: int | None = None) -> float:
    """The room inside a bin (w x d cells, height_u units) from the floor to the rim."""
    wall = binspec.WALL_MM
    iw, idp = binspec.outer_mm(w) - 2 * wall, binspec.outer_mm(d) - 2 * wall
    h = binspec.rim_height_mm(height_u) - binspec.BASE_HEIGHT_MM - binspec.FLOOR_MM
    room = iw * idp * h
    if marker is not None and binspec.style_for_marker(marker) == "corner":
        room -= binspec.POST_MM**2 * h
    else:  # the shelf: its triangle under the top, along the whole width
        s = binspec.SHELF_DEPTH_MM
        room -= iw * (s * 1.6 + min(s, h) ** 2 / 2)
    return max(room, 1.0)


@dataclass
class BinFill:
    """What went into one bin since it was last emptied."""

    count: int = 0
    volume_mm3: float = 0.0  # the items' own volume (estimated)

    def share(self, room_mm3: float, fill: Fill) -> float:
        """How full: 0 empty, 1 full (as far as loose items can fill it)."""
        return self.volume_mm3 / (room_mm3 * fill.packing)

    def more(self, room_mm3: float, fill: Fill, item_mm3: float | None = None) -> int:
        """About how many more items like these fit."""
        each = item_mm3 or (self.volume_mm3 / self.count if self.count else fill.default_mm3)
        left = room_mm3 * fill.packing - self.volume_mm3
        return max(0, int(left / max(each, 1.0)))
