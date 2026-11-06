"""parts.json and vision.yaml: what sortering-pnp needs from screw-synth, as files only."""

from __future__ import annotations

import json

import pytest

from sortering_pnp.files import DEFAULTS, FileStore, find
from sortering_pnp.io import read_json, read_yaml
from sortering_pnp.measure.geometry import PARTS_FORMAT, Geometry, check_parts
from sortering_pnp.measure.infer import Settings
from sortering_pnp.models import ModelStore
from sortering_pnp.vision import shapes_from_parts


def small_parts(**extra):
    m4 = {
        "family": "DIN912",
        "size": "M4",
        "kind": "screw",
        "shape": "socket_cap",
        "mode": "bearing_to_tip",
        "d": 4.0,
        "head_radius": 3.5,
        "head_height": 4.0,
        "headless": False,
        "socket": True,
        "cut_to_length": False,
        "lengths": [20.0, 10.0],
        "heights": {"10": {"head_top": 3.6, "bearing": 3.4, "tip": 2.0}},
    }
    return {"format": PARTS_FORMAT, "version": 1, "classes": {"DIN912_M4": m4}, **extra}


def test_the_shipped_defaults_are_complete():
    parts = read_json(DEFAULTS / "parts.json")
    geo = Geometry.from_parts(parts)
    assert len(geo.classes) >= 60 and "DIN912_M4" in geo and "DIN975_M8" in geo
    m4 = geo["DIN912_M4"]
    assert 16.0 in m4.lengths and m4.mode == "bearing_to_tip" and m4.heights(16)["tip"] > 1.5
    assert geo["DIN7991_M4"].anchor == "head_top" and geo["DIN975_M8"].cut_to_length
    classes = set((read_yaml(DEFAULTS / "classes.yaml") or {})["names"].values())
    assert set(geo.classes) <= classes  # every measured class is a known class
    vision = read_yaml(DEFAULTS / "vision.yaml")
    s = Settings.from_config(vision)
    assert s == Settings()  # the file spells out the defaults
    assert vision["nominal_camera"]["height_mm"] == 300


def test_geometry_and_grip_from_a_parts_table():
    data = small_parts()
    geo = Geometry.from_parts(data)
    g = geo["DIN912_M4"]
    assert g.lengths == [10.0, 20.0] and g.heights(10)["bearing"] == pytest.approx(3.4)
    shape = shapes_from_parts(data)["DIN912_M4"]
    assert (shape.d, shape.head_r, shape.head_k, shape.socket) == (4.0, 3.5, 4.0, True)
    with pytest.raises(ValueError, match="not a parts.json"):
        check_parts({"classes": {}})
    with pytest.raises(ValueError, match="version 2"):
        check_parts(small_parts(version=2))


def test_settings_keep_defaults_for_missing_keys():
    s = Settings.from_config({"measure": {"conf": "0.4", "method": "classical"}})
    assert s.conf == 0.4 and s.method == "classical" and s.crop_size_px == Settings().crop_size_px


def test_the_model_package_brings_its_own_parts_and_classes(machine, tmp_path):
    assert find(machine, "parts") == (DEFAULTS / "parts.json", "sortering-pnp")
    assert find(machine, "vision") == (DEFAULTS / "vision.yaml", "sortering-pnp")
    FileStore(machine.path(machine.paths.files)).save("parts", json.dumps(small_parts()).encode())
    assert find(machine, "parts")[1] == "dropped"
    pkg = tmp_path / "pkg"
    (pkg / "package").mkdir(parents=True)
    (pkg / "package" / "best.onnx").write_bytes(b"onnx")
    card = {"task": "segment", "classes": ["DIN912_M4"], "imgsz": 1024}
    (pkg / "package" / "model_card.json").write_text(json.dumps(card))
    (pkg / "package" / "parts.json").write_text(json.dumps(small_parts()))
    store = ModelStore(machine.path(machine.paths.models), inspect=lambda p: ("segment", [], None))
    sm = store.add_upload(_zip(pkg, tmp_path / "pkg.zip"), "pkg.zip")
    store.set_active("stage1", sm.id)
    path, source = find(machine, "parts")
    assert source == "model" and path.name == "parts.json"
    assert find(machine, "parts", model=False)[1] == "dropped"  # to check models against
    assert find(machine, "classes")[1] == "sortering-pnp"  # the package has none


def _zip(folder, out):
    import zipfile

    with zipfile.ZipFile(out, "w") as z:
        for p in folder.rglob("*"):
            z.write(p, p.relative_to(folder))
    return out
