"""Files to download: one bin, the baseplate, the paper labels, or everything in one zip."""

from __future__ import annotations

import io
import zipfile

from ..layout import Bin, Layout
from ..markers import labels_sheet_svg
from .meshio import stl_bytes, threemf_bytes

PRINT_NOTES = """screwpick printables
====================

Bins (bin_<marker>.3mf): two parts, "bin" and "marker".
- Multi-material printer: give "marker" black filament, "bin" white (or any light colour).
- Single extruder: print everything in white and add a filament change (M600 / "colour change")
  at the layer where the bin's rim ends ({rim} mm for 3-unit bins, height_u x 7 mm in general);
  only the raised black marker cells print after it.
- STL files contain the bin with the marker merged in (one colour; glue a paper label from
  labels.svg onto the shelf instead).

Baseplate (baseplate.stl): Gridfinity baseplate with the working area cut out and a lip around it.
Lay the TPU plate inside the cut-out. A 5 x 5 baseplate is 210 x 210 mm: it fits an Ender 3 bed.

Labels (labels.svg): print at 100 % (no "fit to page"); every black cell must measure 1.6 mm.
"""


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


def baseplate_file(layout: Layout, lip_mm: float = 8.0) -> tuple[str, bytes]:
    from .gridfinity import build_baseplate

    w = layout.work
    mesh = build_baseplate(layout.cols, layout.rows, (w.x, w.y, w.w, w.d), lip_mm)
    return "baseplate.stl", stl_bytes(mesh, "screwpick baseplate")


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
        name, data = baseplate_file(layout)
        z.writestr(name, data)
        name, data = labels_file(layout)
        z.writestr(name, data)
        z.writestr("README.txt", PRINT_NOTES.format(rim=21))
    return buf.getvalue()
