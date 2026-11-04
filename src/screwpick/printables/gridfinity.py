"""Gridfinity bins with a marker shelf, and baseplates with the working area cut out.

Bin (Gridfinity standard foot, no stacking lip): walls, floor, and a 45-degree shelf along the
back wall whose top is flush with the rim and carries the ArUco marker as raised black cells.
Print it in a light colour and switch to black at the rim height (or give the "marker" part its
own filament on a multi-material printer).

Baseplate: open-bottom Gridfinity grid; the working-area cells are cut out (the TPU plate lies on
the bed there) and a low lip around the cut-out stops parts from rolling into the bins.
"""

from __future__ import annotations

import math

import numpy as np
from manifold3d import Manifold

from .. import binspec
from ..markers import marker_bits
from .meshio import Mesh

SEG = 8  # segments per rounded corner


def _rr(w: float, d: float, r: float, z: float, cx: float = 0.0, cy: float = 0.0) -> np.ndarray:
    """Rounded rectangle outline at height z as (N, 3) points."""
    r = max(min(r, w / 2, d / 2), 0.01)
    pts = []
    for qx, qy, a0 in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
        ccx, ccy = cx + qx * (w / 2 - r), cy + qy * (d / 2 - r)
        for i in range(SEG + 1):
            a = math.radians(a0 + 90 * i / SEG)
            pts.append((ccx + r * math.cos(a), ccy + r * math.sin(a), z))
    return np.asarray(pts)


def _frustum(
    levels: list[tuple[float, float, float, float]], cx: float = 0.0, cy: float = 0.0
) -> Manifold:
    """Convex hull of rounded rectangles (w, d, r, z): one straight or 45-degree section."""
    return Manifold.hull_points(np.vstack([_rr(w, d, r, z, cx, cy) for w, d, r, z in levels]))


def _box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> Manifold:
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def _union(parts: list[Manifold]) -> Manifold:
    return Manifold.batch_boolean(parts, _op_add()) if len(parts) > 1 else parts[0]


def _op_add():  # noqa: ANN202 - manifold3d enum
    from manifold3d import OpType

    return OpType.Add


def _to_mesh(m: Manifold) -> Mesh:
    if m.is_empty():
        raise ValueError(f"empty geometry: {m.status()}")
    mesh = m.to_mesh()
    return np.asarray(mesh.vert_properties)[:, :3].copy(), np.asarray(mesh.tri_verts).copy()


def _foot(cx: float, cy: float) -> Manifold:
    """The Gridfinity bin foot of one cell: 0.8 mm at 45 deg, 1.8 mm straight, 2.15 mm at 45 deg."""
    return _union(
        [
            _frustum([(35.6, 35.6, 0.8, 0.0), (37.2, 37.2, 1.6, 0.8)], cx, cy),
            _frustum([(37.2, 37.2, 1.6, 0.8), (37.2, 37.2, 1.6, 2.6)], cx, cy),
            _frustum([(37.2, 37.2, 1.6, 2.6), (41.5, 41.5, 3.75, binspec.BASE_HEIGHT_MM)], cx, cy),
        ]
    )


def build_bin(marker: int, w: int = 1, d: int = 1, height_u: int = 3) -> tuple[Mesh, Mesh]:
    """A bin w x d cells, height_u units tall, with `marker` on its shelf: (body, marker cells).

    Origin: bin centre on the bed; +Y is the back (the shelf side)."""
    if height_u < binspec.MIN_HEIGHT_U:
        raise ValueError(f"bins need at least {binspec.MIN_HEIGHT_U} height units for the shelf")
    W, D = binspec.outer_mm(w), binspec.outer_mm(d)
    H = binspec.rim_height_mm(height_u)
    base = binspec.BASE_HEIGHT_MM
    wall = binspec.WALL_MM
    p = binspec.PITCH_MM
    feet = [_foot((i - (w - 1) / 2) * p, (j - (d - 1) / 2) * p) for i in range(w) for j in range(d)]
    outer = _frustum([(W, D, binspec.BIN_RADIUS_MM, base - 0.01), (W, D, binspec.BIN_RADIUS_MM, H)])
    floor_z = base + binspec.FLOOR_MM
    cavity = _frustum(
        [
            (W - 2 * wall, D - 2 * wall, binspec.BIN_RADIUS_MM - wall, floor_z),
            (W - 2 * wall, D - 2 * wall, binspec.BIN_RADIUS_MM - wall, H + 1.0),
        ]
    )
    # the shelf: flat top at the rim, underside at 45 degrees down to the back wall
    y_back = D / 2 - wall + 0.01
    y_front = y_back - binspec.SHELF_DEPTH_MM
    top_t = 1.6
    z_low = max(floor_z, H - top_t - binspec.SHELF_DEPTH_MM)
    xs = (-W / 2 + wall - 0.01, W / 2 - wall + 0.01)
    shelf = Manifold.hull_points(
        np.array(
            [
                (x, y, z)
                for x in xs
                for (y, z) in ((y_front, H), (y_back, H), (y_back, z_low), (y_front, H - top_t))
            ]
        )
    )
    body = _union([*feet, outer]) - cavity + (shelf ^ outer)
    # the marker: raised black cells on the shelf, row 0 towards the back
    bits = marker_bits(marker)
    n, c = bits.shape[0], binspec.MARKER_CELL_MM
    mx, my = binspec.marker_offset_mm(d)
    half = n * c / 2
    e = 0.01  # cells touching only at a corner overlap a little: a clean, manifold solid
    cells = [
        _box(
            mx - half + col * c - e,
            mx - half + (col + 1) * c + e,
            my + half - (row + 1) * c - e,
            my + half - row * c + e,
            H - 0.01,
            H + binspec.MARKER_INK_MM,
        )
        for row in range(n)
        for col in range(n)
        if bits[row, col]
    ]
    ink = _union(cells)
    return _to_mesh(body - ink), _to_mesh(ink)


def build_baseplate(
    cols: int,
    rows: int,
    work: tuple[int, int, int, int] | None = None,
    lip_mm: float = 8.0,
    lip_wall_mm: float = 2.0,
) -> Mesh:
    """Open-bottom baseplate of cols x rows cells; `work` = (x, y, w, d) cells cut out for the
    working area, with a lip `lip_mm` tall (from the bed) just inside the cut-out.

    Origin: the front-left corner on the bed."""
    p = binspec.PITCH_MM
    h = 4.65
    W, D = cols * p, rows * p
    plate = _frustum([(W, D, 4.0, 0.0), (W, D, 4.0, h)], W / 2, D / 2)
    holes = []
    work_cells = set()
    if work is not None:
        wx, wy, ww, wd = work
        work_cells = {(x, y) for x in range(wx, wx + ww) for y in range(wy, wy + wd)}
    for i in range(cols):
        for j in range(rows):
            if (i, j) in work_cells:
                continue
            cx, cy = (i + 0.5) * p, (j + 0.5) * p
            holes.append(
                _union(
                    [
                        _frustum([(36.3, 36.3, 1.15, -0.01), (37.7, 37.7, 1.85, 0.7)], cx, cy),
                        _frustum([(37.7, 37.7, 1.85, 0.7), (37.7, 37.7, 1.85, 2.5)], cx, cy),
                        _frustum([(37.7, 37.7, 1.85, 2.5), (42.0, 42.0, 4.0, h + 0.01)], cx, cy),
                    ]
                )
            )
    out = plate - _union(holes) if holes else plate
    if work is not None:
        wx, wy, ww, wd = work
        x0, y0, x1, y1 = wx * p, wy * p, (wx + ww) * p, (wy + wd) * p
        out = out - _box(x0, x1, y0, y1, -1.0, h + 1.0)
        lip = _box(x0, x1, y0, y1, 0.0, lip_mm) - _box(
            x0 + lip_wall_mm, x1 - lip_wall_mm, y0 + lip_wall_mm, y1 - lip_wall_mm, -1.0, lip_mm + 1
        )
        out = out + lip
    return _to_mesh(out)
