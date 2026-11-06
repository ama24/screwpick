"""Calibration and real photos taken by sortering_pnp (the only program that moves the printer);
The camera here renders a synthetic ChArUco board."""

from __future__ import annotations

import json
import zipfile

import numpy as np
import pytest

from sortering_pnp.layout import Layout
from sortering_pnp.printer.sim import SimPrinter, SimVision, SimWorld
from sortering_pnp.runner import Runner
from sortering_pnp.transform import Transform2D

cv2 = pytest.importorskip("cv2")
import synthetic_board as synthetic  # noqa: E402

pytest.importorskip("sortering_pnp.camera")


def _runner(machine):
    lay = Layout.default()
    lay.save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    vision = SimVision(world, lambda: lay, Transform2D.identity)
    runner = Runner(machine, SimPrinter(machine, world), vision)
    runner.printer.gcode("G28")
    return runner, vision


def test_camera_calibration_from_the_page(machine):
    runner, vision = _runner(machine)
    spec = runner.photos.spec()
    assert spec.size_mm[0] <= 121 and spec.size_mm[1] <= 107  # lies flat in a 3x3 tray
    assert runner.photos.board_pdf().startswith(b"%PDF")

    cam = synthetic.small_camera()
    rng = np.random.default_rng(0)
    frames = [synthetic.render_board(cam, spec, T, rng) for T in synthetic.intrinsic_views(14, 0)]
    flat = synthetic.pose_matrix(0.0, 0.0, 3.0, (-52.0, -45.0, 300.0))
    frames.append(synthetic.render_board(cam, spec, flat, rng))
    shots = iter(frames)
    moves = []
    vision.grab = lambda: (cv2.cvtColor(next(shots), cv2.COLOR_GRAY2BGR), {"ExposureTime": 1})

    def lock(out, distance):
        out.write_text(json.dumps({"ExposureTime": 5000, "LensPosition": 3.3}))
        return json.loads(out.read_text())

    vision.lock_camera = lock
    runner.photos._park = lambda: moves.append("park")  # noqa: SLF001
    state = runner.photos.lock_camera()
    assert state["locked"] and machine.camera.settings.endswith("camera_settings.json")
    for _ in range(14):
        state = runner.photos.take_view()
    assert state["views"] == 14 and state["views_with_board"] >= 12 and state["coverage"] > 0
    assert moves.count("park") == 2  # once for the lock, once before the hand-held photos
    state = runner.photos.solve_lens()
    assert state["lens"]["rms_px"] < 1.0
    # the old camera-to-printer fit no longer fits the new plate frame
    runner.transform_path.write_text('{"transform": {"A": [[1,0],[0,1]], "t": [0,0]}}')
    runner.setup.mark("camera", "old")
    res = runner.photos.flat_board()
    assert 280 < res["result"]["camera_height_mm"] < 320
    slot = next(s for s in runner.files_state()["slots"] if s["key"] == "calibration")
    assert (
        slot["source"] == "dropped"
        and "1280 x 960 px, with the plate's position" in slot["summary"]
    )
    assert not runner.transform_path.is_file() and not runner.setup.is_done("camera")
    assert any("redo Camera to printer" in e for e in runner.events)
    todo = {t["key"]: t for t in runner.todo_state()["items"]}
    assert todo["calibration"]["done"]
    again = Runner(machine, SimPrinter(machine, SimWorld()), vision)  # the lock is remembered
    assert machine.camera.settings and again.photos.state()["locked"]


def test_measured_square_is_used_and_kept(machine):
    runner, _ = _runner(machine)
    state = runner.calibration_settings(15.2, 1.0)
    assert state["board"]["square_mm"] == pytest.approx(15.2)
    assert state["board_thickness_mm"] == 1.0
    Runner(machine, SimPrinter(machine, SimWorld()), runner.vision)
    assert machine.calibration.measured_square_mm == pytest.approx(15.2)


def test_real_photo_session(machine):
    runner, vision = _runner(machine)
    vision.grab = lambda: (np.full((60, 80, 3), 120, np.uint8), {"ExposureTime": 1})
    parks = []
    runner.photos._park = lambda: parks.append(1)  # noqa: SLF001
    runner.photos.new_session("s01", "alex", "M3 and M4 mix")
    with pytest.raises(ValueError, match="no session"):
        runner.photos.session_photo("nope")
    for _ in range(3):
        res = runner.photos.session_photo("s01", "after a pick")
    assert res["last"] == "img_0003" and len(parks) == 3  # parked before every photo
    (s,) = runner.photos.sessions()
    assert s["id"] == "s01" and s["photos"] == 3 and s["operator"] == "alex"
    z = runner.photos.session_zip("s01")
    names = zipfile.ZipFile(z).namelist()
    assert "s01/session.json" in names and "s01/images/img_0003.png" in names
    assert "s01/images/img_0001.json" in names  # the capture sidecar
