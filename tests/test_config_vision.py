from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from screwpick import config as config_mod
from screwpick.config import ConfigError
from screwpick.vision import parts_from_result, top_height_mm

REPO = Path(__file__).resolve().parents[1]


def test_shipped_config_loads_and_resolves_paths():
    m = config_mod.load(REPO / "configs" / "machine.yaml")
    assert m.printer.driver == "moonraker" and m.picker.mode == "vacuum"
    assert m.path(m.paths.layout) == (REPO / "data" / "layout.yaml").resolve()
    assert m.path(m.paths.model) is None
    assert m.run.shake.axis == "Y"


def test_config_errors(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("picker: {mode: magnet}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="picker.mode"):
        config_mod.load(p)
    p.write_text("printer: {speed: 3}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key 'speed'"):
        config_mod.load(p)


def test_top_heights():
    assert top_height_mm("DIN912_M4", 3.5) == 7.0
    assert top_height_mm("DIN934_M8", None) == pytest.approx(6.4)
    assert top_height_mm("DIN125A_M4", None) == pytest.approx(0.8)
    assert top_height_mm("BRACKET_3030", None) == 30.0
    assert top_height_mm("UNKNOWN", None) == 3.0


def test_parts_from_result():
    result = {
        "objects": [
            {
                "id": 1,
                "class": "DIN912_M4",
                "score": 0.93,
                "polygon": [[0, 0], [10, 0], [10, 5], [0, 5]],
                "position_mm": [50.0, 60.0],
                "orientation_deg": 30.0,
                "length": {"length_mm": 16.0},
                "measurable": True,
                "unknown": False,
            },
            {
                "id": 2,
                "class": "UNKNOWN",
                "score": 0.6,
                "polygon": [],
                "position_mm": [1, 2],
                "unknown": True,
            },
        ]
    }
    heights = []

    def to_plate(px, h):
        heights.append(h)
        return np.c_[px * 0.1, np.full(len(px), h)]

    parts = parts_from_result(result, to_plate, {"DIN912_M4": 3.5}.get)
    a, b = parts
    assert a.length_mm == 16.0 and a.measurable and a.top_mm == 7.0 and heights == [3.5]
    assert a.polygon.shape == (4, 2) and a.polygon[1, 0] == pytest.approx(1.0)
    assert b.unknown and b.length_mm is None and b.polygon.shape == (0, 2)
