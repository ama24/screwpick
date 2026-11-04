"""Write meshes as binary STL or as a 3MF with several parts (one per filament colour).

Only the standard library and numpy: no trimesh needed on the Pi.
"""

from __future__ import annotations

import io
import struct
import zipfile
from collections.abc import Sequence

import numpy as np

Mesh = tuple[np.ndarray, np.ndarray]  # vertices (N, 3) float, triangles (M, 3) int


def stl_bytes(mesh: Mesh, name: str = "screwpick") -> bytes:
    v, f = mesh
    tri = np.asarray(v, dtype=np.float32)[np.asarray(f)]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, norm, out=np.zeros_like(n), where=norm > 0)
    rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tri
    header = name.encode("ascii", "replace")[:80].ljust(80, b" ")
    return header + struct.pack("<I", len(f)) + rec.tobytes()


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)


def _mesh_xml(mesh: Mesh) -> str:
    v, f = mesh
    out = io.StringIO()
    out.write("<mesh><vertices>")
    for x, y, z in np.asarray(v, dtype=float):
        out.write(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>')
    out.write("</vertices><triangles>")
    for a, b, c in np.asarray(f, dtype=int):
        out.write(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')
    out.write("</triangles></mesh>")
    return out.getvalue()


def threemf_bytes(parts: Sequence[tuple[str, Mesh]], name: str = "screwpick") -> bytes:
    """One object made of several parts (e.g. "bin" and "marker"), so a slicer keeps them
    together and each part can get its own filament."""
    return threemf_plate_bytes([(name, parts, (0.0, 0.0))])


def threemf_plate_bytes(
    objects: Sequence[tuple[str, Sequence[tuple[str, Mesh]], tuple[float, float]]],
) -> bytes:
    """Several objects on one build plate, each made of parts and placed at (x, y) mm: a whole
    plate of bins to print at once."""
    res: list[str] = []
    items: list[str] = []
    next_id = 1
    for name, parts, (tx, ty) in objects:
        ids = []
        for pname, mesh in parts:
            res.append(
                f'<object id="{next_id}" name="{_esc(pname)}" type="model">{_mesh_xml(mesh)}</object>'
            )
            ids.append(next_id)
            next_id += 1
        comps = "".join(f'<component objectid="{i}"/>' for i in ids)
        res.append(
            f'<object id="{next_id}" name="{_esc(name)}" type="model">'
            f"<components>{comps}</components></object>"
        )
        items.append(
            f'<item objectid="{next_id}" transform="1 0 0 0 1 0 0 0 1 {tx:.3f} {ty:.3f} 0"/>'
        )
        next_id += 1
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{''.join(res)}</resources>"
        f"<build>{''.join(items)}</build></model>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
