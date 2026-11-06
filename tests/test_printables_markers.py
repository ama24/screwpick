from __future__ import annotations

import io
import zipfile

import numpy as np
import pytest

from sortering_pnp import binspec
from sortering_pnp.layout import Layout
from sortering_pnp.markers import detect, labels_sheet_svg, marker_bits, marker_pose

cv2 = pytest.importorskip("cv2")


def _marker_image(marker: int, cell_px: int = 14, angle: float = 0.0) -> np.ndarray:
    bits = marker_bits(marker)
    n = bits.shape[0]
    img = np.full(((n + 6) * cell_px, (n + 6) * cell_px), 255, np.uint8)
    for r in range(n):
        for c in range(n):
            if bits[r, c]:
                y, x = (r + 3) * cell_px, (c + 3) * cell_px
                img[y : y + cell_px, x : x + cell_px] = 0
    if angle:
        h, w = img.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        img = cv2.warpAffine(img, M, (w, h), borderValue=255)
    return img


def test_marker_bits_have_a_black_border_and_differ():
    a, b = marker_bits(100), marker_bits(101)
    assert a.shape == (binspec.MARKER_CELLS, binspec.MARKER_CELLS)
    assert a[0].all() and a[-1].all() and a[:, 0].all() and a[:, -1].all()
    assert (a != b).any()


@pytest.mark.parametrize("angle", [0.0, 90.0, 33.0])
def test_detect_and_pose(angle):
    img = _marker_image(137, angle=angle)
    seen = detect(img)
    assert [s.marker for s in seen] == [137]
    # pixels -> "plate": x right, y up (like the camera looking down)
    pose = marker_pose(seen[0], lambda px: np.c_[px[:, 0], -px[:, 1], np.zeros(len(px))])
    assert pose.up_deg == pytest.approx(90.0 + angle, abs=1.0)


def test_calibration_board_codes_are_ignored():
    assert detect(_marker_image(7)) == []  # ids below 100 belong to the calibration


def test_labels_sheet():
    svg = labels_sheet_svg([100, 101], {100: "M3 & co"})
    assert svg.startswith("<svg") and "M3 &amp; co" in svg and svg.count('fill="#000"') == 2


tm = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")


def test_bin_geometry_and_marker_cells():
    from sortering_pnp.printables.gridfinity import build_bin

    body, ink = build_bin(100, 1, 1, 3)
    b, k = tm.Trimesh(*body), tm.Trimesh(*ink)
    assert b.is_watertight and k.is_watertight
    lo, hi = b.bounds
    assert np.allclose(lo, [-20.75, -20.75, 0.0]) and np.allclose(hi, [20.75, 20.75, 21.0])
    assert k.bounds[1][2] == pytest.approx(21.0 + binspec.MARKER_INK_MM)
    cell = binspec.MARKER_CELL_MM
    assert k.volume == pytest.approx(
        marker_bits(100).sum() * cell * cell * binspec.MARKER_INK_MM, rel=0.05
    )
    mx, my = binspec.marker_offset_mm(1)
    assert np.allclose(k.bounds.mean(axis=0)[:2], [mx, my], atol=cell)
    with pytest.raises(ValueError):
        build_bin(100, 1, 1, 2)


def test_baseplate_cut_out_and_lip():
    from sortering_pnp.printables.gridfinity import build_baseplate

    v, f = build_baseplate(5, 5, (1, 1, 3, 3), lip_mm=8.0)
    m = tm.Trimesh(v, f)
    assert m.is_watertight
    assert np.allclose(m.bounds, [[0, 0, 0], [210, 210, 8]])
    # nothing inside the working area except the 2 mm lip along its edge (cells 1..3 = 42..168)
    inner = (v[:, 0] > 44.01) & (v[:, 0] < 165.99) & (v[:, 1] > 44.01) & (v[:, 1] < 165.99)
    assert not inner.any()
    lip_top = v[np.isclose(v[:, 2], 8.0)]
    assert np.isclose(lip_top[:, 0], 42.0).any() and np.isclose(lip_top[:, 0], 44.0).any()


def test_export_zip_and_3mf():
    from sortering_pnp.printables import export

    lay = Layout.default()
    lay.bins = lay.bins[:2]
    data = export.everything_zip(lay)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        assert {
            "bin_100.3mf",
            "bin_101.3mf",
            "baseplate_5x5_slim_work3x3.stl",
            "labels.svg",
            "README.txt",
        } <= names
        with zipfile.ZipFile(io.BytesIO(z.read("bin_100.3mf"))) as z3:
            model = z3.read("3D/3dmodel.model").decode()
    assert 'name="bin"' in model and 'name="marker"' in model and "<components>" in model
    name, stl = export.bin_file(lay.bins[0], "stl")
    n = int.from_bytes(stl[80:84], "little")
    assert name == "bin_100.stl" and len(stl) == 84 + 50 * n
