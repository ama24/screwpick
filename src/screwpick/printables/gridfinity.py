"""Gridfinity bins with a marker shelf, and baseplates with the working area cut out.

Bin (Gridfinity standard foot, no stacking lip): walls, floor, and a 45-degree shelf along the
back wall whose top is flush with the rim and carries the ArUco marker as raised black cells,
or (corner style) a square post in the back-right corner, straight down to the floor.
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


def build_bin(
    marker: int, w: int = 1, d: int = 1, height_u: int = 3, style: str | None = None
) -> tuple[Mesh, Mesh]:
    """A bin w x d cells, height_u units tall, with `marker` on its shelf or corner post:
    (body, marker cells). `style` None: the one that goes with the marker id.

    Origin: bin centre on the bed; +Y is the back (the marker side)."""
    style = style or binspec.style_for_marker(marker)
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
    if style == "corner":  # a square post from the floor to the rim: nothing overhangs
        post = binspec.POST_MM
        x_in, y_in = W / 2 - wall - post, D / 2 - wall - post
        holder = _box(x_in, W / 2 + 1.0, y_in, D / 2 + 1.0, base - 0.02, H)
    else:  # the shelf: flat top at the rim, underside at 45 degrees down to the back wall
        y_back = D / 2 - wall + 0.01
        y_front = y_back - binspec.SHELF_DEPTH_MM
        top_t = 1.6
        z_low = max(floor_z, H - top_t - binspec.SHELF_DEPTH_MM)
        xs = (-W / 2 + wall - 0.01, W / 2 - wall + 0.01)
        holder = Manifold.hull_points(
            np.array(
                [
                    (x, y, z)
                    for x in xs
                    for (y, z) in (
                        (y_front, H),
                        (y_back, H),
                        (y_back, z_low),
                        (y_front, H - top_t),
                    )
                ]
            )
        )
    body = _union([*feet, outer]) - cavity + (holder ^ outer)
    # the marker: raised black cells on the shelf or post, row 0 towards the back
    bits = marker_bits(marker)
    n, c = bits.shape[0], binspec.MARKER_CELL_MM
    mx, my = binspec.marker_offset_mm(d, w, style)
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
    slim: bool = False,
) -> Mesh:
    """Open-bottom baseplate of cols x rows cells; `work` = (x, y, w, d) cells cut out for the
    working area, with a lip `lip_mm` tall (from the bed) just inside the cut-out.

    slim: only 2.5 mm tall. It keeps the bottom chamfer and the straight part of each socket,
    which is what holds a bin's foot in place; the flared top (2.15 mm) is left out. The bins
    stand on the bed either way (open bottom), so their height does not change.

    Origin: the front-left corner on the bed."""
    p = binspec.PITCH_MM
    h = binspec.BASEPLATE_SLIM_MM if slim else binspec.BASEPLATE_MM
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
            socket = [
                _frustum([(36.3, 36.3, 1.15, -0.01), (37.7, 37.7, 1.85, 0.7)], cx, cy),
                _frustum([(37.7, 37.7, 1.85, 0.7), (37.7, 37.7, 1.85, min(2.5, h) + 0.01)], cx, cy),
            ]
            if not slim:
                socket.append(
                    _frustum([(37.7, 37.7, 1.85, 2.5), (42.0, 42.0, 4.0, h + 0.01)], cx, cy)
                )
            holes.append(_union(socket))
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


def _sockets(
    cols: int, rows: int, h: float, slim: bool, ox: float = 0.0, oy: float = 0.0
) -> list[Manifold]:
    """The bin sockets of a cols x rows grid whose front-left corner is at (ox, oy)."""
    p = binspec.PITCH_MM
    out = []
    for i in range(cols):
        for j in range(rows):
            cx, cy = ox + (i + 0.5) * p, oy + (j + 0.5) * p
            socket = [
                _frustum([(36.3, 36.3, 1.15, -0.01), (37.7, 37.7, 1.85, 0.7)], cx, cy),
                _frustum([(37.7, 37.7, 1.85, 0.7), (37.7, 37.7, 1.85, min(2.5, h) + 0.01)], cx, cy),
            ]
            if not slim:
                socket.append(
                    _frustum([(37.7, 37.7, 1.85, 2.5), (42.0, 42.0, 4.0, h + 0.01)], cx, cy)
                )
            out.append(_union(socket))
    return out


def bed_grid_origin(cols: int, rows: int, bed) -> tuple[float, float]:  # noqa: ANN001 - beds.Bed
    """Where the grid's front-left corner lies on a bed plate: centred (in Y between the front
    edge and the kept-free strip at the back)."""
    p = binspec.PITCH_MM
    w, d = bed.size
    d -= bed.back_keep_mm
    if cols * p > w or rows * p > d:
        raise ValueError(f"a {cols} x {rows} grid does not fit on the {bed.name} bed")
    return (w - cols * p) / 2, (d - rows * p) / 2


def cuts(length: float, first_line: float, n_cells: int, limit: float) -> list[float]:
    """Where to cut a plate `length` long (cell lines start at `first_line`) so that no piece is
    longer than `limit`: always on a cell line, as few cuts as possible."""
    p = binspec.PITCH_MM
    lines = [first_line + k * p for k in range(1, n_cells)]
    if length <= limit:
        return []
    two = [x for x in lines if x <= limit and length - x <= limit]
    if two:  # one cut is enough: the most even one (sturdier pieces)
        return [min(two, key=lambda x: max(x, length - x))]
    out: list[float] = []
    start = 0.0
    while length - start > limit:
        fits = [x for x in lines if start < x <= start + limit]
        if not fits:
            raise ValueError(f"no cell line within {limit:g} mm: the pieces cannot be printed")
        out.append(max(fits))
        start = out[-1]
    return out


def build_bed_baseplate(
    cols: int,
    rows: int,
    bed,
    slim: bool = True,
    margin_mm: float = 6.0,  # noqa: ANN001
) -> list[tuple[str, Mesh, tuple[float, float, float, float]]]:
    """The grid with a border out to the bed's edges (binder clips hold it), cut on cell lines
    into pieces that print on that bed. Returns (position name, mesh, (x0, y0, x1, y1)); all
    pieces in bed coordinates, so they fit together as they lie."""
    h = binspec.BASEPLATE_SLIM_MM if slim else binspec.BASEPLATE_MM
    w = bed.size[0]
    d = bed.size[1] - bed.back_keep_mm
    gx, gy = bed_grid_origin(cols, rows, bed)
    c = bed.corner_cut_mm
    plate = _box(0, w, 0, d, 0, h)
    if c > 0:
        notches = [
            _box(x0, x0 + c, y0, y0 + c, -1, h + 1)
            for x0 in (-0.01, w - c + 0.01)
            for y0 in (-0.01, d - c + 0.01)
        ]
        plate = plate - _union(notches)
    plate = plate - _union(_sockets(cols, rows, h, slim, gx, gy))
    xs = [0.0, *cuts(w, gx, cols, bed.printable[0] - margin_mm), w]
    ys = [0.0, *cuts(d, gy, rows, bed.printable[1] - margin_mm), d]
    pieces = []
    for j in range(len(ys) - 1):
        for i in range(len(xs) - 1):
            box = _box(xs[i] - 0.01, xs[i + 1] + 0.01, ys[j] - 0.01, ys[j + 1] + 0.01, -1, h + 1)
            pieces.append(
                (
                    _piece_name(i, j, len(xs) - 1, len(ys) - 1),
                    _to_mesh(plate ^ box),
                    (xs[i], ys[j], xs[i + 1], ys[j + 1]),
                )
            )
    return pieces


def _piece_name(i: int, j: int, nx: int, ny: int) -> str:
    col = {1: [""], 2: ["left", "right"]}.get(nx, [f"c{k + 1}" for k in range(nx)])[i]
    row = {1: [""], 2: ["front", "back"]}.get(ny, [f"r{k + 1}" for k in range(ny)])[j]
    return "-".join(x for x in (row, col) if x) or "whole"
