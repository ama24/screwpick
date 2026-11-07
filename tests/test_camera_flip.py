"""A camera mounted the other way round: the camera itself turns its picture (photos and the
live picture), set from the page and kept; Camera to printer has to be done again."""

from __future__ import annotations

import copy
import sys
import types

from sortering_pnp.camera import rpicam_still
from sortering_pnp.layout import Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


def _runner(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    return Runner(machine, SimPrinter(machine, world), vision)


def test_turn_180_is_kept_and_asks_for_camera_to_printer_again(machine):
    runner = _runner(machine)
    reopened = []
    runner.vision.reopen_camera = lambda: reopened.append(True)
    runner.setup.mark("camera")
    runner.calib_pairs = [{"marker": 100, "plate": [0, 0], "bed": [1, 1]}]
    assert (machine.camera.hflip, machine.camera.vflip) == (True, True)  # turned by default
    st = runner.camera_flip(False, False)
    assert st["flip"] == {"h": False, "v": False} and reopened == [True]
    assert not runner.setup.is_done("camera") and runner.calib_pairs == []
    runner.camera_flip(False, False)  # unchanged: nothing reset again
    assert reopened == [True]
    fresh = copy.deepcopy(machine)  # after a restart
    fresh.camera.hflip = fresh.camera.vflip = True
    again = Runner(fresh, SimPrinter(fresh, SimWorld()), runner.vision)
    assert (again.m.camera.hflip, again.m.camera.vflip) == (False, False)


def test_the_camera_gets_the_flip(monkeypatch):
    seen = {}

    class FakePicam:
        camera_controls = {}

        def create_still_configuration(self, **kw):
            seen.update(kw)
            return kw

        def configure(self, cfg): ...
        def set_controls(self, c): ...
        def start(self): ...
        def stop(self): ...
        def close(self): ...

    class Transform:
        def __init__(self, hflip=0, vflip=0):
            self.hflip, self.vflip = hflip, vflip

    monkeypatch.setitem(sys.modules, "picamera2", types.SimpleNamespace(Picamera2=FakePicam))
    monkeypatch.setitem(sys.modules, "libcamera", types.SimpleNamespace(Transform=Transform))
    monkeypatch.setattr("sortering_pnp.camera.time.sleep", lambda s: None)
    from sortering_pnp.camera import Camera

    Camera(None, hflip=True, vflip=True)
    t = seen["transform"]
    assert (t.hflip, t.vflip) == (1, 1)
    seen.clear()
    Camera(None)
    assert "transform" not in seen
    assert rpicam_still(True, True)[-2:] == ["--hflip", "--vflip"]
    assert "--hflip" not in rpicam_still()
