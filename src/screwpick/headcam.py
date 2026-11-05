"""A camera on the tool head (optional, camera.source: head): it sees a small patch at a time,
so the program moves it over the area to look at and joins the photos.

- Sorting: only the tray (the plate bin) is photographed, in a few overlapping photos.
- Resync, the OK after a bin change and the start of a run: the whole grid and one cell around
  it, so bins put next to the grid are found too.

Calibration is the same as for the fixed camera, done at printer.photo_position (the head over
the tray, at the photo height): markers seen by the camera, and the tip on each marker. Every
other photo was taken with the head moved by d from there: what it sees is shifted by d on the
bed, so its plate coordinates are shifted by A^-1 d (A: the calibrated plate -> bed map). All
photos then share the frame of the calibration photo, and everything after that (printer X/Y,
bins, grip points) works as with the fixed camera.

The camera sits higher above the parts than the fixed one is far from them in pixels per mm,
so it needs its own model, trained at its scale (the Models tab: role "head camera").
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .markers import MarkerPose
from .parts import Part


@dataclass
class HeadCamera:
    fov_mm: list[float] = field(default_factory=lambda: [80.0, 60.0])  # one photo, X x Y
    offset: list[float] = field(default_factory=lambda: [0.0, 0.0])  # image centre - tip, X/Y
    overlap: float = 0.25  # neighbouring photos overlap this share (parts on the seam)
    margin_cells: int = 1  # Resync: look this many cells beyond the grid, for new bins
    same_mm: float = 3.0  # a part in two photos: the same one when its centres are this close


def tiles(
    rect: tuple[float, float, float, float], fov: Sequence[float], overlap: float
) -> list[tuple[float, float]]:
    """Camera centres covering `rect` (x0, y0, x1, y1), row by row in a zigzag (short moves)."""
    x0, y0, x1, y1 = rect
    out: list[tuple[float, float]] = []

    def centres(a: float, b: float, size: float) -> list[float]:
        span = b - a
        if span <= size:
            return [(a + b) / 2]
        n = math.ceil((span - size) / (size * (1 - overlap))) + 1
        step = (span - size) / (n - 1)
        return [a + size / 2 + i * step for i in range(n)]

    xs, ys = centres(x0, x1, fov[0]), centres(y0, y1, fov[1])
    for j, y in enumerate(ys):
        for x in xs if j % 2 == 0 else reversed(xs):
            out.append((x, y))
    return out


def shift_part(p: Part, d: np.ndarray) -> None:
    """Move a part's plate coordinates by d (mm)."""
    p.plate_xy = (p.plate_xy[0] + d[0], p.plate_xy[1] + d[1])
    if len(p.polygon):
        p.polygon = np.asarray(p.polygon, dtype=float) + d
    if p.grip_plate is not None:
        p.grip_plate = (p.grip_plate[0] + d[0], p.grip_plate[1] + d[1])
    if p.com_plate is not None:
        p.com_plate = (p.com_plate[0] + d[0], p.com_plate[1] + d[1])


def merge_parts(parts: list[Part], same_mm: float) -> list[Part]:
    """One entry per part: seen in two photos (same class, centres close), the surer one stays.
    A part cut by a photo's edge is usually seen whole in the next one: it has the higher score
    and the larger outline, so it wins."""
    keep: list[Part] = []
    for p in sorted(parts, key=lambda q: (-q.score, -len(q.polygon))):
        if any(q.cls == p.cls and math.dist(q.plate_xy, p.plate_xy) <= same_mm for q in keep):
            continue
        keep.append(p)
    for i, p in enumerate(keep, start=1):
        p.id = i
    return keep


def merge_markers(markers: list[MarkerPose]) -> list[MarkerPose]:
    """A marker seen in several photos: the mean of its positions (and directions)."""
    by: dict[int, list[MarkerPose]] = {}
    for m in markers:
        by.setdefault(m.marker, []).append(m)
    out = []
    for mk, ms in sorted(by.items()):
        c = np.mean([m.centre for m in ms], axis=0)
        a = math.degrees(
            math.atan2(
                sum(math.sin(math.radians(m.up_deg)) for m in ms),
                sum(math.cos(math.radians(m.up_deg)) for m in ms),
            )
        )
        out.append(MarkerPose(mk, (float(c[0]), float(c[1])), a))
    return out
