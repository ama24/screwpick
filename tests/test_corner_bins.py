"""Corner bins: the same sizes with the marker on a square post in the back-right corner
(codes 50-99) instead of a shelf. The camera tells the two apart by the code and finds both."""

from __future__ import annotations

import numpy as np
import pytest

from sortering_pnp import binspec
from sortering_pnp.layout import Bin, Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


def test_corner_sizes_have_their_own_codes():
    seen: set[int] = set()
    for s in binspec.SIZES:
        assert not seen & set(s.markers), f"{s.name} overlaps another size"
        seen |= set(s.markers)
    assert seen == set(range(50, 240))
    assert binspec.size_for_marker(90).name == "3x3x3u-corner"
    assert binspec.size_for_marker(97).name == "4x4x3u-corner"
    assert binspec.size_named("1x1x3u-corner").markers == range(50, 66)
    assert [binspec.style_for_marker(m) for m in (49, 50, 99, 100, 245)] == [
        "shelf",
        "corner",
        "corner",
        "shelf",
        "shelf",
    ]
    shelf = {(s.w, s.d, s.height_u) for s in binspec.SIZES if s.style == "shelf"}
    corner = {(s.w, s.d, s.height_u) for s in binspec.SIZES if s.style == "corner"}
    assert shelf == corner  # every size comes in both styles


@pytest.mark.parametrize(
    ("rot", "corner"), [(0, (1, 1)), (90, (-1, 1)), (180, (-1, -1)), (270, (1, -1))]
)
def test_the_code_sits_in_the_turned_corner(rot, corner):
    """A 3x3 corner tray turned by `rot`: its code is in that corner of the bed, and the plate
    area is the whole inside except the post."""
    lay = Layout(origin=[0.0, 0.0], cols=3, rows=3)
    lay.bins = [Bin(marker=90, x=0, y=0, w=3, d=3, rot=rot)]
    lay.set_work(90, auto=True)
    mx, my = lay.marker_centre(lay.bins[0])
    off = 126 / 2 - 0.25 - 1.2 - binspec.POST_MM / 2
    assert (mx, my) == pytest.approx((63 + corner[0] * off, 63 + corner[1] * off))
    inner = 0.25 + 1.2
    assert lay.work_rect() == pytest.approx((inner, inner, 126 - inner, 126 - inner))
    assert not lay.in_work((mx, my))  # on the post
    assert lay.in_work((63 - corner[0] * off, 63 - corner[1] * off))  # the opposite corner
    assert lay.in_work((mx, 63.0)) and lay.in_work((63.0, my))  # beside the post: free


def test_mixed_bins_resync_through_the_camera(machine):
    """Shelf and corner bins together, turned every way: Resync puts each where it stands."""
    real = Layout(origin=[17.2, 8.9], cols=6, rows=5)
    real.bins = [
        Bin(marker=90, x=2, y=0, w=3, d=3, rot=90),  # corner tray, post at the back-left
        Bin(marker=140, x=0, y=0, w=2, d=1, rot=90),  # shelf 1x2, lying across
        Bin(marker=66, x=0, y=1, w=1, d=2, rot=180),  # corner 1x2
        Bin(marker=50, x=5, y=4, rot=270),
        Bin(marker=51, x=5, y=3),
        Bin(marker=100, x=0, y=4, rot=180),
        Bin(marker=86, x=2, y=3, w=2, d=2, rot=0),  # corner 2x2
    ]
    lay = Layout.default()
    world = SimWorld()
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: real, Transform2D.identity)
    )
    lay.save(machine.path(machine.paths.layout))
    runner.setup_home()
    res = runner.scan_bins()
    assert res["problems"] == [] and res["work"] == 90
    saved = runner.layout()
    assert saved.origin == pytest.approx(real.origin, abs=0.05)
    assert {(b.marker, b.x, b.y, b.w, b.d, b.rot) for b in saved.bins} == {
        (b.marker, b.x, b.y, b.w, b.d, b.rot) for b in real.bins
    }


def test_corner_tray_wins_over_an_equal_shelf_tray():
    lay = Layout()
    seen = [(220, (5 + 63.0, 5 + 63.0), 0.0), (90, (5 + 189.0, 5 + 63.0), 0.0)]
    lay.resync(seen)
    assert lay.work_marker == 90  # the same size: the lower code, the corner tray


tm = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")


def test_corner_bin_geometry_has_no_overhang():
    from sortering_pnp.printables.gridfinity import build_bin

    body, ink = build_bin(90, 3, 3, 3)
    b, k = tm.Trimesh(*body), tm.Trimesh(*ink)
    assert b.is_watertight and k.is_watertight
    assert b.bounds[1][2] == pytest.approx(21.0)
    mx, my = binspec.marker_offset_mm(3, 3, "corner")
    assert mx > 40 and my > 40  # back-right corner
    assert np.allclose(k.bounds.mean(axis=0)[:2], [mx, my], atol=binspec.MARKER_CELL_MM)
    # the post is as big at the floor as at the rim: nothing for a part to hide under
    floor = binspec.BASE_HEIGHT_MM + binspec.FLOOR_MM

    def solid(mesh, z):  # area of solid material at height z (a 0.2 mm slice)
        from manifold3d import Manifold
        from manifold3d import Mesh as MMesh

        m = Manifold(MMesh(mesh[0].astype(np.float32), mesh[1].astype(np.uint32)))
        return (m ^ Manifold.cube([300, 300, 0.2]).translate([-150, -150, z])).volume() / 0.2

    assert solid(body, floor + 0.5) == pytest.approx(solid(body, 20.5), rel=0.01)
    sb, _ = build_bin(220, 3, 3, 3)  # the shelf tray: more material at the top than below
    assert solid(sb, 20.5) > solid(sb, floor + 0.5) * 1.5
