"""The Calibrate button: one job that locks, takes the lens photos by itself while the board is
moved about, solves the lens and measures the board lying flat in the tray."""

from __future__ import annotations

import copy
import json

import cv2
import numpy as np
import pytest

import synthetic_board as synthetic
from sortering_pnp.layout import Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D


def _runner(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    runner = Runner(machine, SimPrinter(machine, world), vision)
    runner.printer.gcode("G28")
    return runner, vision


def _camera(vision, script):
    """What the camera sees, frame after frame; a full-resolution photo is the frame of now."""
    seq = iter(script)
    now = {"img": None}

    def frame():
        now["img"] = cv2.cvtColor(next(seq), cv2.COLOR_GRAY2BGR)
        return now["img"]

    vision.grab = lambda: (now["img"], {"ExposureTime": 1})

    def lock(out, distance):
        out.write_text(json.dumps({"ExposureTime": 5000, "LensPosition": 3.3}))
        return json.loads(out.read_text())

    vision.lock_camera = lock
    return frame


def test_one_button_runs_every_step(machine):
    runner, vision = _runner(machine)
    spec = runner.photos.spec()
    cam = synthetic.small_camera()
    rng = np.random.default_rng(0)
    held = [synthetic.render_board(cam, spec, T, rng) for T in synthetic.intrinsic_views(20, 0)]
    flat = synthetic.render_board(
        cam, spec, synthetic.pose_matrix(0.0, 0.0, 3.0, (-52.0, -45.0, 300.0)), rng
    )
    # each held place for 6 frames (it holds still there), then the board lies in the tray
    script = [img for img in held for _ in range(6)] + [flat] * 40
    frame = _camera(vision, script)
    runner.photos._park = lambda: None  # noqa: SLF001
    runner.photos.start_calibration(frame=frame, min_views=12, coverage=0.3, pause_s=0.0)
    job = runner.photos.job
    job._thread.join(120)  # noqa: SLF001
    st = runner.photos.state()
    assert st["job"]["phase"] == "done", st["job"]
    assert st["job"]["views"] >= 12 and st["views"] == st["job"]["views"]  # taken by itself
    assert st["locked"] and st["lens"]["rms_px"] < 1.0
    assert st["calibrated"] == "dropped"  # calibration.json is in the Files tab, used at once
    assert 280 < st["job"]["result"]["camera_height_mm"] < 320
    assert "Camera to printer" in st["job"]["message"]


def test_a_place_already_taken_is_not_taken_again(machine):
    runner, vision = _runner(machine)
    spec = runner.photos.spec()
    rng = np.random.default_rng(1)
    one = synthetic.render_board(
        synthetic.small_camera(), spec, synthetic.intrinsic_views(1, 0)[0], rng
    )
    frame = _camera(vision, [one] * 60)
    runner.photos._park = lambda: None  # noqa: SLF001
    runner.photos.start_calibration(frame=frame, min_views=12, pause_s=0.0)
    runner.photos.job._thread.join(60)  # noqa: SLF001 - ends when the frames run out
    assert runner.photos.state()["views"] == 1  # the board never moved: one photo


def test_stop_and_the_live_picture(machine):
    runner, vision = _runner(machine)
    blank = np.full((960, 1280), 90, np.uint8)
    frame = _camera(vision, [blank] * 100000)
    runner.photos._park = lambda: None  # noqa: SLF001
    runner.photos.start_calibration(frame=frame, pause_s=0.01)
    with pytest.raises(ValueError, match="already running"):
        runner.photos.start_calibration(frame=frame)
    vision.frame = frame  # the page's live picture: the frame with the job's drawing on it
    jpg = runner.photos.live_jpeg()
    assert jpg[:2] == b"\xff\xd8"
    runner.photos.cancel_calibration()
    runner.photos.job._thread.join(10)  # noqa: SLF001
    assert runner.photos.state()["job"]["phase"] == "cancelled"
    crop = cv2.imdecode(np.frombuffer(runner.photos.live_jpeg("crop"), np.uint8), cv2.IMREAD_COLOR)
    assert crop is not None  # not ready (no camera calibration): a picture that says so


def test_the_simulator_has_no_live_picture(machine):
    runner, _ = _runner(machine)
    with pytest.raises(ValueError, match="no live picture"):
        runner.photos.start_calibration()
    assert runner.photos.live_jpeg()[:2] == b"\xff\xd8"  # a picture saying so


def test_camera_cropping_is_tried_then_saved(machine):
    runner, _ = _runner(machine)
    st = runner.crop_state()
    assert st["colour"] == "auto" and not st["changed"]
    with pytest.raises(ValueError):
        runner.crop_preview(1.0, "pink")
    st = runner.crop_preview(-2.0, "#FFFFFF")
    assert st["margin_mm"] == -2.0 and st["colour"] == "#FFFFFF" and st["changed"]
    assert runner.m.camera.mask_margin_mm == 0.0  # only tried
    runner.crop_save(-2.0, "#FFFFFF")
    fresh = copy.deepcopy(machine)  # as after a restart: machine.yaml's values, then the saved
    fresh.camera.mask_colour, fresh.camera.mask_margin_mm = "auto", 0.0
    again = Runner(fresh, SimPrinter(fresh, SimWorld()), runner.vision)
    assert again.m.camera.mask_colour == "#FFFFFF" and again.m.camera.mask_margin_mm == -2.0


def test_the_live_picture_over_http(machine):
    import threading
    import urllib.request

    from sortering_pnp.web.server import make_server

    runner, _ = _runner(machine)
    srv = make_server(runner, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/camera/live.jpg", timeout=10) as r:
            assert r.headers["Content-Type"] == "image/jpeg" and r.read(2) == b"\xff\xd8"
        with urllib.request.urlopen(base + "/api/camera/live.mjpg?view=crop", timeout=10) as r:
            assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
            head = r.read(120)
            assert head.startswith(b"--frame\r\nContent-Type: image/jpeg") and b"\xff\xd8" in head
        with urllib.request.urlopen(base + "/api/crop", timeout=10) as r:
            assert json.loads(r.read())["colour"] == "auto"
    finally:
        srv.shutdown()
