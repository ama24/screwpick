"""Files to download: one bin, plates of bins to print together, the baseplate, paper labels, the
whole ready-made set (prints/ in the repository), or everything for a layout in one zip."""

from __future__ import annotations

import io
import math
import zipfile
from collections.abc import Callable
from pathlib import Path

from .. import binspec
from ..binspec import SIZES, BinSize
from ..layout import Bin, Layout
from ..markers import labels_sheet_svg
from .meshio import stl_bytes, threemf_bytes, threemf_plate_bytes

BED_MM = (220.0, 220.0)  # Ender 3: plates of bins are laid out for this bed
PLATE_GAP_MM = 8.0

PRINT_NOTES = """screwpick printables
====================

Bins: two parts, "bin" and "marker" (the code on the shelf, or on the corner post).
- Multi-material printer: give "marker" black filament, "bin" white (or any light colour).
- Single extruder: print in white and add a filament change (M600 / "colour change") at the
  rim height: 21 mm for 3u bins, 42 mm for 6u bins (height units x 7 mm). Only the raised
  black marker cells print after it. All bins on one plate have the same height: one change.
- STL files contain the bin with the marker merged in (one colour; glue a paper label from
  the labels sheet onto the shelf instead).

The code number tells screwpick the bin's size, and where its code sits:
{sizes}

Two styles of the same sizes: "shelf" bins carry the code on a 45-degree shelf along the back;
"corner" bins (codes 50-99) on a square post in the back-right corner, straight down to the
floor. Nothing overhangs in a corner bin, so no part gets trapped under it: use a corner tray
as the plate, where the parts are thrown in and spread.

Baseplate: a plain Gridfinity grid, any size and anywhere on the bed (Resync finds it). The
slim one is 2.5 mm thin (the standard one 4.65 mm); bins stand on the bed in both. 5 x 5 is
210 x 210 mm: the most an Ender 3 bed takes. The plate is a tray bin (3x3 or 4x4) on the grid:
lay a TPU sheet cut to its floor inside; the parts are thrown in there.

Labels: print at 100 % (no "fit to page"); every black cell must measure 1.6 mm.
"""


def sizes_text() -> str:
    return (
        "\n".join(f"  {s.markers.start}-{s.markers.stop - 1}: {s.name:15s} {s.use}" for s in SIZES)
        + "\n  240-249: any other size (set it in the layout), shelf style"
    )


def _bin_meshes(b: Bin):  # noqa: ANN202
    from .gridfinity import build_bin

    return build_bin(b.marker, b.w, b.d, b.height_u)


def bin_file(b: Bin, fmt: str = "3mf") -> tuple[str, bytes]:
    body, ink = _bin_meshes(b)
    if fmt == "3mf":
        return f"bin_{b.marker}.3mf", threemf_bytes(
            [("bin", body), ("marker", ink)], f"bin {b.marker}"
        )
    if fmt == "stl":
        import numpy as np

        v = np.vstack([body[0], ink[0]])
        f = np.vstack([body[1], ink[1] + len(body[0])])
        return f"bin_{b.marker}.stl", stl_bytes((v, f), f"screwpick bin {b.marker}")
    raise ValueError(f"unknown format {fmt!r} (3mf or stl)")


def plate_grid(size: BinSize, bed: tuple[float, float] = BED_MM) -> tuple[int, int]:
    """How many bins of this size fit on the bed (columns, rows)."""
    w, d = binspec.outer_mm(size.w), binspec.outer_mm(size.d)
    cols = max(1, math.floor((bed[0] + PLATE_GAP_MM) / (w + PLATE_GAP_MM)))
    rows = max(1, math.floor((bed[1] + PLATE_GAP_MM) / (d + PLATE_GAP_MM)))
    return cols, rows


def plate_file(
    size: BinSize, markers: list[int], bed: tuple[float, float] = BED_MM
) -> tuple[str, bytes]:
    """One build plate full of bins of one size (and height: one colour change for all)."""
    cols, rows = plate_grid(size, bed)
    if len(markers) > cols * rows:
        raise ValueError(f"{len(markers)} {size.name} bins do not fit on one plate ({cols * rows})")
    w, d = binspec.outer_mm(size.w), binspec.outer_mm(size.d)
    n_rows = math.ceil(len(markers) / cols)
    span_x = min(len(markers), cols) * (w + PLATE_GAP_MM) - PLATE_GAP_MM
    span_y = n_rows * (d + PLATE_GAP_MM) - PLATE_GAP_MM
    x0 = (bed[0] - span_x) / 2 + w / 2
    y0 = (bed[1] - span_y) / 2 + d / 2
    objects = []
    for i, m in enumerate(markers):
        body, ink = _bin_meshes(Bin(marker=m, x=0, y=0, w=size.w, d=size.d, height_u=size.height_u))
        pos = (x0 + (i % cols) * (w + PLATE_GAP_MM), y0 + (i // cols) * (d + PLATE_GAP_MM))
        objects.append((f"bin {m}", [("bin", body), ("marker", ink)], pos))
    name = f"bins_{size.name}_{markers[0]}-{markers[-1]}.3mf"
    return name, threemf_plate_bytes(objects)


BED_NOTES = """screwpick baseplate for the {name}
{line}

The {cols} x {rows} grid with a flat border out to the bed's edges ({w:g} x {d:g} mm), so it can be
clamped down with binder clips like a glass plate. It is cut on cell lines into {n} pieces
that each print on this printer: lay them out as named (front-left at the bed's front-left
corner) and put at least one clip on every outer edge of every piece.

{note}.
The grid's front-left corner lies {gx:g} mm from the left edge and {gy:g} mm from the front;
Resync finds it anyway. Corner notches ({c:g} mm) clear the bed's corner screws.
Slim (2.5 mm) or standard (4.65 mm): both hold the bins the same way.
"""


def bed_baseplate_files(
    bed_key: str, slim: bool = True, cols: int = 5, rows: int = 5
) -> list[tuple[str, bytes]]:
    """The pieces of a baseplate that covers a whole printer bed, plus how to lay them out."""
    from .beds import bed
    from .gridfinity import bed_grid_origin, build_bed_baseplate

    b = bed(bed_key)
    kind = "slim" if slim else "standard"
    pieces = build_bed_baseplate(cols, rows, b, slim=slim)
    files = [
        (
            f"baseplate_{b.key}_{kind}_{name}.stl",
            stl_bytes(mesh, f"screwpick {b.key} baseplate {kind} {name}"),
        )
        for name, mesh, _ in pieces
    ]
    gx, gy = bed_grid_origin(cols, rows, b)
    title = f"{b.name} ({kind})"
    notes = BED_NOTES.format(
        name=title,
        line="=" * (len(title) + 30),
        cols=cols,
        rows=rows,
        w=b.size[0],
        d=b.size[1] - b.back_keep_mm,
        n=len(pieces),
        note=b.note[0].upper() + b.note[1:],
        gx=round(gx, 2),
        gy=round(gy, 2),
        c=b.corner_cut_mm,
    )
    files.append((f"baseplate_{b.key}_{kind}_README.txt", notes.encode()))
    return files


def bed_baseplate_zip(bed_key: str, slim: bool = True, cols: int = 5, rows: int = 5) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in bed_baseplate_files(bed_key, slim, cols, rows):
            z.writestr(name, data)
    return buf.getvalue()


def baseplate_file(layout: Layout, lip_mm: float = 8.0, slim: bool = True) -> tuple[str, bytes]:
    from .gridfinity import build_baseplate

    kind = "slim" if slim else "standard"
    name = f"baseplate_{layout.cols}x{layout.rows}_{kind}"
    cut = None
    if layout.work_bin() is None:  # an older layout: the working area is cut out of the plate
        w = layout.work
        cut = (w.x, w.y, w.w, w.d)
        name += f"_work{w.w}x{w.d}"
    mesh = build_baseplate(layout.cols, layout.rows, cut, lip_mm, slim=slim)
    return name + ".stl", stl_bytes(mesh, f"screwpick baseplate {kind}")


def labels_file(layout: Layout, markers: list[int] | None = None) -> tuple[str, bytes]:
    ids = markers or [b.marker for b in layout.bins]
    names = {b.marker: (b.label or ", ".join(b.holds) or b.role) for b in layout.bins}
    return "labels.svg", labels_sheet_svg(ids, names).encode()


def everything_zip(layout: Layout) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for b in layout.bins:
            name, data = bin_file(b, "3mf")
            z.writestr(name, data)
        for slim in (True, False):
            name, data = baseplate_file(layout, slim=slim)
            z.writestr(name, data)
        name, data = labels_file(layout)
        z.writestr(name, data)
        z.writestr("README.txt", PRINT_NOTES.format(sizes=sizes_text()))
    return buf.getvalue()


def catalogue(out: Path, log: Callable[[str], None] = print) -> list[Path]:
    """The ready-made set in prints/: every bin of every size, as full build plates, both
    baseplates for the default layout, label sheets per size, and the notes."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def put(rel: str, data: bytes) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
        log(f"{rel} ({len(data) / 1e6:.2f} MB)")

    from .mounts import mount_files

    for name, data in mount_files():  # screen holder and camera arm for the top bar
        put(f"mounts/{name}", data)
    for size in SIZES:
        cols, rows = plate_grid(size)
        per = cols * rows
        ids = list(size.markers)[: size.in_catalogue]  # trays: a couple are plenty
        for k in range(0, len(ids), per):
            name, data = plate_file(size, ids[k : k + per])
            put(f"bins/{size.name}/{name}", data)
        put(f"labels/labels_{size.name}.svg", labels_sheet_svg(list(size.markers)).encode())
    lay = Layout.default()  # 5 x 5: the most an Ender 3 bed takes; the page makes any size
    for slim in (True, False):
        name, data = baseplate_file(lay, slim=slim)
        put(f"baseplate/{name}", data)
    from .beds import BEDS

    for key in BEDS:  # the same grid, clamped to the whole bed
        for slim in (True, False):
            for name, data in bed_baseplate_files(key, slim, lay.cols, lay.rows):
                put(f"baseplate/{key}/{name}", data)
    put("PRINTING.txt", PRINT_NOTES.format(sizes=sizes_text()).encode())
    return written
