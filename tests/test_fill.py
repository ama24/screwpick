"""How full the bins are: item volume from the outline and height, a bin's room from its size."""

from __future__ import annotations

import numpy as np
import pytest

from sortering_pnp.fill import (
    BinFill,
    CatalogueVolumes,
    Fill,
    Packing,
    bin_room_mm3,
    item_volume_mm3,
    part_volume_mm3,
    polygon_area,
)
from sortering_pnp.layout import Layout
from sortering_pnp.parts import Part
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


def _part(w: float, length: float, top: float) -> Part:
    poly = np.array([(0, 0), (length, 0), (length, w), (0, w)], dtype=float)
    return Part(1, "X", 0.9, (0.0, 0.0), 0.0, poly, top)


def test_item_volume_from_outline_and_height():
    f = Fill()
    assert polygon_area(np.array([(0, 0), (4, 0), (4, 20), (0, 20)], float)) == 80.0
    # an M4 x 16 screw lying down: about 4 x 20 mm outline, 7 mm high (the head)
    v = item_volume_mm3(_part(4.0, 20.0, 7.0), f)
    assert 200 < v < 400  # the real one is about 300 mm3
    assert item_volume_mm3(_part(0, 0, 0), f) == f.default_mm3  # nothing measured


def test_bin_room_scales_with_the_gridfinity_size():
    one = bin_room_mm3(1, 1, 3, 100)
    assert bin_room_mm3(1, 2, 3, 140) == pytest.approx(2.2 * one, rel=0.15)
    assert bin_room_mm3(1, 1, 6, 160) > 2.4 * one  # same floor, twice the walls and more
    assert bin_room_mm3(1, 1, 3, 50) > one  # a corner post takes less room than a shelf


def test_bin_fill_share_and_more():
    room = 10_000.0
    bf = BinFill(count=10, volume_mm3=2_000.0)
    assert bf.share(room, 0.5) == pytest.approx(0.4)
    assert bf.more(room, 0.5) == 15  # 3000 mm3 left, 200 each


def _items(*rows):
    return {
        "format": "screwsynth-items",
        "version": 1,
        "items": [{"id": i, "class": c, "length": n, "volume_mm3": v} for i, c, n, v in rows],
    }


def test_catalogue_volumes():
    cat = CatalogueVolumes.from_items(
        _items(
            ("DIN912_M4x10", "DIN912_M4", 10.0, 200.0),
            ("DIN912_M4x20", "DIN912_M4", 20.0, 300.0),
            ("DIN934_M4", "DIN934_M4", None, 99.0),
            ("UNKNOWN_WOODSCREW_01", "UNKNOWN", None, 500.0),
        )
    )
    assert len(cat) == 3  # unknown items are all different: not averaged
    assert cat.volume("DIN912_M4", 10) == 200.0  # the part number
    assert cat.volume("DIN912_M4", 16) == pytest.approx(260.0)  # between two lengths
    assert cat.volume("DIN912_M4", 40) == pytest.approx(600.0)  # beyond: scaled
    assert cat.volume("DIN912_M4", None) == 250.0  # length not measured: the class average
    assert cat.volume("DIN934_M4", None) == 99.0 and cat.volume("NOPE", 3) is None
    f = Fill()
    known = _part(4.0, 20.0, 7.0)
    known.cls, known.length_mm = "DIN912_M4", 20.0
    assert part_volume_mm3(known, f, cat) == 300.0
    known.cls = "SOMETHING_NEW"  # not catalogued: from its outline
    assert part_volume_mm3(known, f, cat) == item_volume_mm3(known, f)


def test_packing_is_learnt_per_family():
    pk = Packing()
    assert pk.for_family("DIN912", 0.3) == 0.3  # nothing learnt: the setting
    pk.add("DIN125A", 0.55)
    assert pk.for_family("DIN125A", 0.3) == 0.55
    assert pk.for_family("DIN912", 0.3) == 0.55  # no own samples: everything learnt so far
    pk.add("DIN912", 0.2)
    pk.add("DIN912", 0.3)
    assert pk.for_family("DIN912", 0.3) == pytest.approx(0.25)


def test_cell_names():
    lay = Layout()
    assert lay.cell_name(0, 0) == "A1" and lay.cell_name(1, 5) == "B6"
    assert lay.cell_name(26, 0) == "AA1"
    b = Layout.default().bins[0]
    assert Layout.default().bin_name(b) == "A1"


def test_sorting_counts_and_fills_the_bins(machine, layout):
    machine.run.shake.enabled = False
    path = machine.path(machine.paths.layout)
    layout.save(path)
    world = SimWorld(seed=5)
    world.populate(layout, n=8)
    runner = Runner(
        machine,
        SimPrinter(machine, world),
        SimVision(world, lambda: Layout.load(path), Transform2D.identity),
    )
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done", runner.state.message
    fill = runner.bins_state()["fill"]
    used = {k: v for k, v in fill.items() if v["count"]}
    assert sum(v["count"] for v in used.values()) == 8
    for v in used.values():
        assert 0 < v["share"] < 1 and v["more"] > 0 and v["cell"][0].isalpha()
    again = Runner(machine, SimPrinter(machine, world), runner.vision)  # kept across restarts
    assert again.bins_state()["fill"] == fill
