"""Bin sizes by marker, build plates of bins, the slim baseplate, the ready-made prints/ set, and
the Pi lock file."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

from screwpick import binspec
from screwpick.layout import Bin, Layout

ROOT = Path(__file__).resolve().parents[1]
NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


def test_size_ranges():
    seen: set[int] = set()
    for s in binspec.SIZES:
        assert not seen & set(s.markers), f"{s.name} overlaps another size"
        seen |= set(s.markers)
    assert seen == set(range(50, 240))  # 50-99: the corner-post style
    assert (
        binspec.size_for_marker(225).name == "3x3x3u"
        and binspec.size_for_marker(235).name == "4x4x3u"
    )
    assert binspec.size_for_marker(105).name == "1x1x3u"
    assert binspec.size_for_marker(185).name == "1x2x6u"
    assert binspec.size_for_marker(245) is None
    names = [binspec.size_for_marker(b.marker).name for b in Layout.default().bins]
    assert names == ["1x1x3u"] * 16 + ["3x3x3u"]  # 16 sorting bins around the plate tray


def test_new_bins_get_their_size_from_the_marker():
    lay = Layout()
    b = lay.place_from_marker(185, (5 + 42 * 0.5, 5 + 42 * 1.0), yaw_deg=0.0)
    assert (b.w, b.d, b.height_u) == (1, 2, 6) and (b.x, b.y) == (0, 0)
    lay.bins.append(Bin(marker=120, x=4, y=4, height_u=6))
    assert any("printed on 1x1x3u bins" in p for p in lay.problems())


def _plate_boxes(data: bytes) -> list[tuple[np.ndarray, np.ndarray]]:
    root = ET.fromstring(zipfile.ZipFile(BytesIO(data)).read("3D/3dmodel.model"))
    meshes, comps = {}, {}
    for o in root.find("m:resources", NS):
        mesh = o.find("m:mesh", NS)
        if mesh is not None:
            verts = mesh.find("m:vertices", NS)
            meshes[o.get("id")] = np.array([[float(v.get(a)) for a in "xyz"] for v in verts])
        else:
            comps[o.get("id")] = [c.get("objectid") for c in o.find("m:components", NS)]
    boxes = []
    for item in root.find("m:build", NS):
        t = np.array([float(x) for x in item.get("transform").split()])[9:]
        pts = np.vstack([meshes[i] for i in comps[item.get("objectid")]]) + t
        boxes.append((pts.min(axis=0), pts.max(axis=0)))
    return boxes


pytest.importorskip("manifold3d")


@pytest.mark.parametrize(("name", "count"), [("1x1x3u", 16), ("1x2x3u", 8), ("2x2x6u", 4)])
def test_plates_fill_an_ender_bed_without_touching(name, count):
    from screwpick.printables.export import plate_file, plate_grid

    size = binspec.size_named(name)
    cols, rows = plate_grid(size)
    assert cols * rows == count
    fname, data = plate_file(size, list(size.markers)[:count])
    assert fname == f"bins_{name}_{size.markers.start}-{size.markers.start + count - 1}.3mf"
    boxes = _plate_boxes(data)
    assert len(boxes) == count
    lo = np.min([b[0] for b in boxes], axis=0)
    hi = np.max([b[1] for b in boxes], axis=0)
    assert lo[0] >= 0 and lo[1] >= 0 and hi[0] <= 220 and hi[1] <= 220
    assert hi[2] == pytest.approx(binspec.rim_height_mm(size.height_u) + binspec.MARKER_INK_MM)
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            gap = np.maximum(b[0][:2] - a[1][:2], a[0][:2] - b[1][:2]).max()
            assert gap >= 7.9


def test_slim_baseplate():
    trimesh = pytest.importorskip("trimesh")
    from screwpick.printables.gridfinity import build_baseplate

    for slim, h in ((True, 2.5), (False, 4.65)):
        v, f = build_baseplate(5, 5, (1, 1, 3, 3), lip_mm=8.0, slim=slim)
        m = trimesh.Trimesh(v, f)
        assert m.is_watertight
        ribs = v[v[:, 2] > 1e-6]
        ribs = ribs[(ribs[:, 0] < 40) | (ribs[:, 0] > 170)]  # outside the working area's lip
        assert ribs[:, 2].max() == pytest.approx(h)


def test_catalogue_matches_the_committed_prints(tmp_path):
    from screwpick.printables.export import catalogue

    files = catalogue(tmp_path, log=lambda s: None)
    made = sorted(p.relative_to(tmp_path).as_posix() for p in files)
    committed = sorted(
        p.relative_to(ROOT / "prints").as_posix()
        for p in (ROOT / "prints").rglob("*")
        if p.is_file() and p.name != "README.md"
    )
    assert made == committed, "regenerate: screwpick print catalogue --out prints"
    assert "160-179: 1x1x6u" in (tmp_path / "PRINTING.txt").read_text()


def test_pi_lock_pins_everything_with_hashes():
    text = (ROOT / "requirements-pi.lock.txt").read_text(encoding="utf-8")
    reqs = re.findall(
        r"^([a-z0-9_.-]+)==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64})+)", text, re.M
    )
    names = {n for n, _, _ in reqs}
    body = [ln for ln in text.splitlines() if ln and not ln.startswith(("#", "--", " "))]
    assert len(body) == len(reqs), "every requirement needs ==version and --hash"
    assert {"torch", "torchvision", "ultralytics", "ncnn", "numpy", "manifold3d"} <= names
    assert "opencv-contrib-python-headless" in names and "opencv-python" not in names
    versions = {n: v for n, v, _ in reqs}
    assert versions["torch"].endswith("+cpu")  # the build that runs on a Pi


@pytest.mark.parametrize("key", ["ender3", "mk3"])
@pytest.mark.parametrize("slim", [True, False])
def test_bed_baseplates_cover_the_bed_and_print_in_pieces(key, slim):
    trimesh = pytest.importorskip("trimesh")
    from screwpick.printables.beds import bed
    from screwpick.printables.gridfinity import bed_grid_origin, build_bed_baseplate

    b = bed(key)
    pieces = build_bed_baseplate(5, 5, b, slim=slim)
    assert len(pieces) == 4
    h = binspec.BASEPLATE_SLIM_MM if slim else binspec.BASEPLATE_MM
    lo = np.min([trimesh.Trimesh(*m).bounds[0] for _, m, _ in pieces], axis=0)
    hi = np.max([trimesh.Trimesh(*m).bounds[1] for _, m, _ in pieces], axis=0)
    assert lo == pytest.approx([0, 0, 0], abs=0.02)
    assert hi == pytest.approx([b.size[0], b.size[1] - b.back_keep_mm, h], abs=0.02)
    gx, gy = bed_grid_origin(5, 5, b)
    for name, mesh, (x0, y0, x1, y1) in pieces:
        m = trimesh.Trimesh(*mesh)
        assert m.is_watertight, name
        assert x1 - x0 <= b.printable[0] - 6 and y1 - y0 <= b.printable[1] - 6  # prints
        # cut on cell lines of the grid, never through a socket
        for edge in (x0, x1):
            assert edge in (0.0, b.size[0]) or ((edge - gx) / 42.0) % 1 == pytest.approx(0)
        for edge in (y0, y1):
            assert edge in (0.0, b.size[1] - b.back_keep_mm) or (
                (edge - gy) / 42.0
            ) % 1 == pytest.approx(0)
    names = sorted(n for n, _, _ in pieces)
    assert names == ["back-left", "back-right", "front-left", "front-right"]


def test_bed_baseplate_zip():
    from screwpick.printables.export import bed_baseplate_zip

    with zipfile.ZipFile(BytesIO(bed_baseplate_zip("ender3", slim=True))) as z:
        names = z.namelist()
        assert len([n for n in names if n.endswith(".stl")]) == 4
        assert "12.5 mm from the left edge" in z.read("baseplate_ender3_slim_README.txt").decode()
    with pytest.raises(ValueError, match="unknown bed"):
        bed_baseplate_zip("voron")
