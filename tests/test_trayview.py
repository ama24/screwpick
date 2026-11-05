"""Only the tray goes to the model: the rest of the photo is painted over, its size kept."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from screwpick.layout import Layout
from screwpick.printer.sim import SimPart, SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D
from screwpick.trayview import WorkArea, paint_outside, parse_colour, rect

cv2 = pytest.importorskip("cv2")


def test_paint_outside_keeps_the_tray_and_paints_the_rest():
    img = np.zeros((100, 120, 3), np.uint8)
    img[:] = (10, 20, 30)  # the bins around
    img[20:80, 30:90] = (90, 120, 60)  # the tray's floor
    keep = rect(30, 20, 89, 79)
    post = rect(80, 20, 89, 29)  # the corner post
    out = paint_outside(img, keep, [post])
    assert out.shape == img.shape and (img[0, 0] == (10, 20, 30)).all()  # a copy
    assert tuple(out[50, 60]) == (90, 120, 60)  # the floor is kept
    assert tuple(out[5, 5]) == (90, 120, 60)  # outside: painted the floor's colour
    assert tuple(out[24, 85]) == (90, 120, 60)  # the post too
    white = paint_outside(img, keep, [], parse_colour("#FFFFFF"))
    assert tuple(white[5, 5]) == (255, 255, 255)
    assert parse_colour("auto") is None and parse_colour("#102030") == (0x30, 0x20, 0x10)
    with pytest.raises(ValueError):
        parse_colour("blue")


def test_work_area_contains():
    w = WorkArea(rect(0, 0, 100, 100), [rect(80, 80, 100, 100)])
    assert w.contains((50, 50)) and not w.contains((90, 90)) and not w.contains((150, 50))
    assert w.shifted(np.array([200.0, 0.0])).contains((250, 50))


def test_painted_where_the_tray_is_in_the_real_camera_model(machine):
    from screwsynth.calibration.geometry import (
        Camera,
        nominal_camera,
        roi_camera_matrix,
        roi_for_area,
    )
    from screwsynth.measure.infer import Scene

    from screwpick.vision import CameraVision

    full = nominal_camera()
    roi = roi_for_area(full, (0.0, 0.0), (220.0, 220.0), 5.0)
    cam = Camera(roi_camera_matrix(full.K, roi), full.dist, (roi["w"], roi["h"]), full.T_cam_plate)
    img = np.full((roi["h"], roi["w"], 3), 40, np.uint8)
    scene = Scene(img, cam, roi, "nominal")
    work = WorkArea(rect(50, 50, 170, 160))
    machine.camera.mask_colour = "#FFFFFF"
    out = CameraVision._painted(SimpleNamespace(m=machine), scene, work)

    def px(x, y):
        u, v = cam.project(np.array([[x, y, 0.0]]))[0]
        return out.image[int(v), int(u)]

    assert out.image.shape == img.shape  # same size: same scale for the model
    assert tuple(px(110, 105)) == (40, 40, 40)  # inside the tray: as photographed
    assert tuple(px(20, 20)) == (255, 255, 255)  # a bin: painted
    assert out.gray[0, 0] == pytest.approx(1.0)  # the grey copy is painted too


def test_the_model_sees_only_the_tray_except_for_the_bin_check(machine):
    machine.run.shake.enabled = False
    lay = Layout.default()
    lay.bins = [b for b in lay.bins if b.marker in (103, 104)] + [lay.work_bin()]
    for b in lay.bins:
        if b.role != "work":
            b.role = "wild"
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    x0, y0, _, _ = lay.work_rect()
    world.parts = [SimPart("DIN934_M4", x0 + 30, y0 + 30, 0.0, None, (7.0, 7.0), 3.2)]
    cx, cy = lay.bin_centre(lay.bin_by_marker(104))
    world.parts.append(SimPart("DIN934_M4", cx, cy - 8, 0.0, None, (7.0, 7.0), 3.2))  # in a bin
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    runner = Runner(machine, SimPrinter(machine, world), vision)
    runner.printer.gcode("G28")
    shot = runner.take_shot(lay, Transform2D.identity())
    assert vision.painted[-1] and len(shot.parts) == 1  # the nut in the bin is not seen
    shot = runner.take_shot(lay, Transform2D.identity(), area="bins", paint=False)
    assert not vision.painted[-1] and len(shot.parts) == 2  # the bin check sees into bins
    machine.camera.mask = False
    runner.take_shot(lay, Transform2D.identity())
    assert not vision.painted[-1]
