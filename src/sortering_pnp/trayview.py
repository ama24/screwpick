"""Only the tray goes to the model: everything outside the tray's inside (the walls, the bins
around it, their codes and the parts in them) and the corner post are painted over with the
colour of the tray's floor before the model looks at the photo.

The photo keeps its size and so its scale: the model sees the parts exactly as large as in its
training pictures (a plate with the parts, cut at the frame's edge where the lip is). Cropping
and resizing instead would change the scale and spoil the classes and the lengths.

The markers are read from the photo itself, never from the painted one; the check after a bin
change (are there parts left in a bin?) looks at the photo unpainted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class WorkArea:
    """The tray's inside in the plate frame (mm, at the floor: z = 0), and holes in it."""

    outline: np.ndarray  # (N, 2)
    holes: list[np.ndarray] = field(default_factory=list)  # e.g. the corner post

    def shifted(self, d: np.ndarray) -> WorkArea:
        return WorkArea(self.outline + d, [h + d for h in self.holes])

    def contains(self, xy: tuple[float, float]) -> bool:
        def inside(poly: np.ndarray) -> bool:
            x, y = xy
            n, c = len(poly), False
            for i in range(n):
                (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
                if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                    c = not c
            return c

        return inside(self.outline) and not any(inside(h) for h in self.holes)


def rect(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    return np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], dtype=float)


def parse_colour(text: str) -> tuple[int, int, int] | None:
    """A colour "#RRGGBB" -> BGR; "auto" -> None (the floor's own colour)."""
    t = text.strip().lstrip("#")
    if text.strip().lower() == "auto":
        return None
    if len(t) != 6:
        raise ValueError(f"camera.mask_colour: auto or #RRGGBB, not {text!r}")
    r, g, b = (int(t[i : i + 2], 16) for i in (0, 2, 4))
    return b, g, r


def paint_outside(
    image: np.ndarray,
    keep_px: np.ndarray,
    holes_px: list[np.ndarray],
    colour: tuple[int, int, int] | None = None,
) -> np.ndarray:
    """A copy of `image` (BGR) with everything outside `keep_px` and inside `holes_px` painted:
    `colour`, or the median colour of what is kept (the tray's floor: the parts are few)."""
    import cv2

    mask = np.zeros(image.shape[:2], np.uint8)
    cv2.fillPoly(mask, [np.round(keep_px).astype(np.int32)], 255)
    for h in holes_px:
        cv2.fillPoly(mask, [np.round(h).astype(np.int32)], 0)
    if colour is None:
        floor = image[mask > 0]
        colour = tuple(int(v) for v in np.median(floor, axis=0)) if len(floor) else (200,) * 3
    out = image.copy()
    out[mask == 0] = colour
    return out
