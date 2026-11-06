"""Without a model the camera still photographs and reads the bin codes (calibration, Resync);
only the parts need the model, and sorting says so."""

from __future__ import annotations

import cv2
import numpy as np

from sortering_pnp.calib.geometry import nominal_from_config
from sortering_pnp.markers import marker_bits
from sortering_pnp.vision import CameraVision


def bare_vision(machine, out):
    """A CameraVision without a camera and without a model (as on a new Pi)."""
    v = object.__new__(CameraVision)
    v.m, v.out, v.analyzer, v.model_error = machine, out, None, None
    v.calibration, v.nominal = None, nominal_from_config({})
    v.cam_full = v.nominal
    return v


def test_codes_are_read_and_circled_without_a_model(machine, tmp_path):
    v = bare_vision(machine, tmp_path)
    w, h = v.nominal.image_size
    img = np.full((h, w), 230, np.uint8)
    cell = 40
    code = np.where(np.kron(marker_bits(120), np.ones((cell, cell))) > 0, 0, 255).astype(np.uint8)
    y0, x0 = h // 2 - code.shape[0] // 2, w // 2 - code.shape[1] // 2
    img[y0 : y0 + code.shape[0], x0 : x0 + code.shape[1]] = code
    photo = tmp_path / "p.jpg"
    cv2.imwrite(str(photo), img)

    shot = v.analyse(photo, "p", lambda mid: 0.0)
    assert shot.parts == [] and [mk.marker for mk in shot.markers] == [120]
    assert shot.vis == shot.view and shot.view.is_file()  # the plain photo for the page
    # the circle on the page lands on the code: its centre in the view photo's pixels
    view = cv2.imread(str(shot.view), cv2.IMREAD_GRAYSCALE)
    u, v_ = shot.markers[0].view_px
    u, v_ = int(u), int(v_)
    assert view[v_ - 12 : v_ + 12, u - 12 : u + 12].min() < 100  # black cells of the code here
    assert view[v_ - 3 : v_ + 3, u + 150 : u + 156].min() > 200  # and plain background beside
    assert "Models tab" in v.model_missing()
