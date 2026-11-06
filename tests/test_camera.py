"""The Pi camera helpers (camera.py): focus, the locked-settings check, coverage, saving."""

import cv2
import numpy as np
import pytest

from sortering_pnp import camera
from sortering_pnp.io import read_json


def test_pi_focus():
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, (200, 300)).astype(np.uint8)
    blurred = cv2.GaussianBlur(sharp, (0, 0), 2)
    assert camera.focus_metric(sharp) > camera.focus_metric(blurred)
    pos = [2.8, 2.9, 3.0, 3.1, 3.2, 3.3]
    scores = [-((p - 3.07) ** 2) for p in pos]
    assert camera.best_focus(pos, scores) == pytest.approx(3.07, abs=1e-6)


def test_pi_settings_check():
    locked = {
        "ExposureTime": 10000,
        "AnalogueGain": 1.5,
        "ColourGains": [2.0, 1.6],
        "LensPosition": 3.3,
        "size": [4608, 2592],
    }
    good = {
        "ExposureTime": 10020,
        "AnalogueGain": 1.51,
        "ColourGains": (2.0, 1.605),
        "LensPosition": 3.3,
        "size": [4608, 2592],
    }
    assert camera.settings_mismatch(locked, good) == []
    bad = dict(good, ExposureTime=12000, ColourGains=(2.3, 1.6))
    problems = camera.settings_mismatch(locked, bad)
    assert len(problems) == 2 and any("ExposureTime" in p for p in problems)
    c = camera.controls_from_settings(locked)
    assert c["AeEnable"] is False and c["AwbEnable"] is False and c["AfMode"] == 0


def test_pi_coverage_and_save(tmp_path):
    cov = camera.Coverage((4608, 2592), (16, 9))
    cov.add(np.array([[10.0, 10.0], [4600, 2590], [2300, 1300]]))
    assert cov.fraction == pytest.approx(3 / 144)
    assert cov.ascii().count("\n") == 8
    p = camera.save_capture(
        tmp_path, "x", np.zeros((4, 4, 3), np.uint8), {"ExposureTime": 1, "ColourGains": (1.0, 2.0)}
    )
    assert p.is_file() and read_json(p.with_suffix(".json"))["metadata"]["ColourGains"] == [
        1.0,
        2.0,
    ]
