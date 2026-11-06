"""Skip the camera calibration for now: the camera is taken as straight down over the picture's
centre from a height set on the page; everything else (Camera to printer, cropping) works."""

from __future__ import annotations

import copy

import pytest

from sortering_pnp.layout import Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D
from sortering_pnp.vision import CameraVision


def _runner(machine, vision=None):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    vision = vision or SimVision(world, lambda: lay, Transform2D.identity)
    return Runner(machine, SimPrinter(machine, world), vision)


def test_the_height_is_used_and_kept(machine):
    runner = _runner(machine)
    reloads = []
    runner.vision.reload_setup = lambda: reloads.append(machine.camera.height_mm)
    st = runner.photos.state()
    assert not st["calibrated"] and not st["height_set"]
    with pytest.raises(ValueError, match="100 to 1000"):
        runner.camera_height(20.0)
    runner.camera_height(362.0)
    assert reloads == [362.0]  # the vision setup follows at once
    assert runner.photos.state()["height_set"]
    fresh = copy.deepcopy(machine)  # after a restart: machine.yaml's value, then the page's
    fresh.camera.height_mm = None
    again = Runner(fresh, SimPrinter(fresh, SimWorld()), runner.vision)
    assert again.m.camera.height_mm == 362.0


def test_the_nominal_camera_takes_the_height(machine, tmp_path):
    v = object.__new__(CameraVision)
    v.m, v.out, v.store = machine, tmp_path, None
    v.use_models = lambda choice: None  # no model needed here
    machine.camera.height_mm = 362.0
    v.reload_setup()
    assert v.calibration is None and v.nominal_height_mm == 362.0
    assert v.cam_full.T_cam_plate[2, 3] == pytest.approx(362.0)  # straight down, 362 mm up
    machine.camera.height_mm = None
    v.reload_setup()
    assert v.nominal_height_mm == 300.0  # vision.yaml's


def test_cropping_needs_no_camera_calibration(machine):
    runner = _runner(machine)
    assert "camera calibration" not in " ".join(runner.crop_state()["missing"])
