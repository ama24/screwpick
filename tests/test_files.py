"""The Files tab: files from the other projects, dropped in and kept here."""

from __future__ import annotations

import json

import pytest

from screwpick.files import SLOTS, FileStore, find, slots_state
from screwpick.layout import Layout
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D

ITEMS = {
    "format": "screwsynth-items",
    "version": 1,
    "items": [{"id": "DIN934_M4", "class": "DIN934_M4", "length": None, "volume_mm3": 99.0}],
}
CALIB = {
    "camera_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    "dist_coeffs": [0] * 5,
    "image_size": [4608, 2592],
}


def test_every_slot_says_where_what_and_without():
    for s in SLOTS:
        assert s.where and s.used_for and s.without and s.filename


def test_store_checks_keeps_and_removes(tmp_path):
    st = FileStore(tmp_path / "files")
    with pytest.raises(ValueError, match="not an items.json"):
        st.save("items", b'{"hello": 1}')
    with pytest.raises(ValueError, match="cannot be read"):
        st.save("settings", b"a: [unclosed")
    with pytest.raises(ValueError, match="no file slot"):
        st.save("nope", b"{}")
    info = st.save("items", json.dumps(ITEMS).encode(), "items (3).json")
    assert info["summary"] == "1 items in 1 classes" and info["original"] == "items (3).json"
    assert st.path("items").name == "items.json" and st.added("items")["bytes"] > 0
    st.remove("items")
    assert st.path("items") is None and st.added("items") is None


def test_which_copy_is_used(machine, tmp_path):
    own = tmp_path / "my_calibration.json"
    own.write_text(json.dumps(CALIB))
    assert find(machine, "items") == (None, "none")  # never read from the other project
    machine.paths.calibration = str(own)
    assert find(machine, "calibration") == (own, "machine.yaml")
    FileStore(machine.path(machine.paths.files)).save("calibration", json.dumps(CALIB).encode())
    path, source = find(machine, "calibration")
    assert source == "dropped" and path.name == "calibration.json"
    state = {s["key"]: s for s in slots_state(machine)}
    assert state["calibration"]["summary"] == "4608 x 2592 px, without the plate's position"


def test_dropping_items_json_is_used_at_once(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    runner = Runner(
        machine, SimPrinter(machine, world), SimVision(world, lambda: lay, Transform2D.identity)
    )
    assert runner.volumes is None  # nothing dropped: estimated from the outlines
    state = runner.add_file("items", json.dumps(ITEMS).encode(), "items.json")
    assert runner.volumes is not None and runner.volumes.volume("DIN934_M4", None) == 99.0
    assert next(s for s in state["slots"] if s["key"] == "items")["source"] == "dropped"
    runner.remove_file("items")
    assert runner.volumes is None
