"""Parts on the plate as the sorter sees them: class, where (plate mm and printer X/Y), how high,
how long, and how much free space is around them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .transform import Transform2D


@dataclass
class Part:
    id: int
    cls: str  # class name, e.g. DIN912_M4 (UNKNOWN for unknown parts)
    score: float
    plate_xy: tuple[float, float]  # centre, plate frame mm
    plate_angle: float  # long axis, plate frame deg
    polygon: np.ndarray  # outline, plate frame mm (N, 2)
    top_mm: float  # highest point above the plate
    length_mm: float | None = None  # snapped (or rounded, for rod) length; None = not measured
    measurable: bool = False
    unknown: bool = False
    bed_xy: tuple[float, float] = (0.0, 0.0)  # printer X/Y (set by to_bed)
    bed_angle: float = 0.0
    gap_mm: float = math.inf  # free space to the nearest other part (<= 0: touching)
    extra: dict = field(default_factory=dict)


def to_bed(parts: Sequence[Part], T: Transform2D) -> None:
    for p in parts:
        bx, by = T.apply(np.asarray(p.plate_xy, dtype=float))
        p.bed_xy = (float(bx), float(by))
        p.bed_angle = T.rotate_angle(p.plate_angle) % 180.0


def _inside(points: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Point-in-polygon (crossing number) for many points."""
    x, y = points[:, 0][:, None], points[:, 1][:, None]
    x0, y0 = poly[:, 0][None, :], poly[:, 1][None, :]
    x1, y1 = np.roll(poly[:, 0], -1)[None, :], np.roll(poly[:, 1], -1)[None, :]
    cond = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return (cond & (x < xc)).sum(axis=1) % 2 == 1


def _point_edges(points: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Distance of each point to the polygon's outline."""
    a = poly[None, :, :]
    b = np.roll(poly, -1, axis=0)[None, :, :]
    p = points[:, None, :]
    ab = b - a
    t = np.clip(((p - a) * ab).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-12), 0.0, 1.0)
    proj = a + t[..., None] * ab
    return np.linalg.norm(p - proj, axis=-1).min(axis=1)


def polygon_gap(a: np.ndarray, b: np.ndarray) -> float:
    """Distance between two outlines; 0 when they touch or overlap."""
    if len(a) < 3 or len(b) < 3:
        return math.inf
    if _inside(a, b).any() or _inside(b, a).any():
        return 0.0
    return float(min(_point_edges(a, b).min(), _point_edges(b, a).min()))


def compute_gaps(parts: Sequence[Part], horizon_mm: float = 20.0) -> None:
    """Fill gap_mm of every part (only neighbours within `horizon_mm` are measured exactly)."""
    boxes = [
        (p.polygon.min(axis=0), p.polygon.max(axis=0)) if len(p.polygon) else (None, None)
        for p in parts
    ]
    for p in parts:
        p.gap_mm = math.inf
    for i, pi in enumerate(parts):
        for j in range(i + 1, len(parts)):
            pj = parts[j]
            (lo_i, hi_i), (lo_j, hi_j) = boxes[i], boxes[j]
            if lo_i is None or lo_j is None:
                continue
            sep = np.maximum(np.maximum(lo_j - hi_i, lo_i - hi_j), 0.0)
            if float(np.linalg.norm(sep)) > horizon_mm:
                continue
            g = polygon_gap(pi.polygon, pj.polygon)
            pi.gap_mm = min(pi.gap_mm, g)
            pj.gap_mm = min(pj.gap_mm, g)
