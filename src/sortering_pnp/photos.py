"""Photos for the other projects, taken by sortering_pnp: the only program that moves the printer.

The camera (camera.py, camlock.py) locks the settings, grabs a frame and saves it with its
metadata; the calibration maths are in calib/, the session format in session.py (screw-synth
reads both). The bed is parked by sortering_pnp's own motion (photo position, soft limits, busy checks), so nothing
else ever sends G-code.

Camera calibration (Calibrate tab), in order:
  1. the board: a ChArUco board small enough to lie flat inside the tray (7 x 6 squares of 15 mm:
     105 x 90 mm, for the 3x3 tray), printed from the page; measure a square and enter it;
  2. lock the camera: exposure, gain, white balance and focus are frozen for every photo;
  3. lens: 25-40 photos of the board held in different places and tilts (the whole frame);
  4. position: the board flat in the tray (where the parts lie); the plate frame starts at the
     board, so the camera-to-printer step must be redone afterwards.
The result is a calibration.json in the Files tab, used at once.

Real photos (Photos tab): sessions in the format screw-synth reads (data/real/<session>/ with session.json,
inventory.csv, images/img_NNNN.png + sidecar), downloaded as a zip for labelling on the PC.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .io import read_json, write_json


@dataclass
class CalibrationBoard:
    squares_x: int = 7  # 105 x 90 mm: lies flat inside a 3x3 tray (and next to a corner post)
    squares_y: int = 6
    square_mm: float = 15.0  # printed size; the measured one goes in measured_square_mm
    marker_mm: float = 11.0
    dictionary: str = "DICT_5X5_100"


@dataclass
class Calibration:
    board: CalibrationBoard = field(default_factory=CalibrationBoard)
    measured_square_mm: float | None = None  # measure a printed square with a caliper
    board_thickness_mm: float = 0.0  # paper + backing: the board lies on the TPU sheet
    lens_distance_mm: float = 300.0  # roughly camera to plate: where the focus sweep starts
    area_mm: list[float] = field(default_factory=lambda: [220.0, 220.0])  # the model's view
    margin_mm: float = 5.0


class Photos:
    def __init__(self, runner: Any) -> None:
        self.r = runner
        self.m = runner.m

    # ------------------------------------------------------------------ places
    @property
    def root(self) -> Path:
        p = self.m.path(self.m.paths.captures)
        assert p is not None
        return p

    def _cam_key(self) -> str:
        return "head_calibration" if self.m.camera.source == "head" else "calibration"

    def _dir(self) -> Path:
        return self.root / self._cam_key()

    def spec(self) -> Any:
        from sortering_pnp.calib.board import BoardSpec

        b = self.m.calibration.board
        spec = BoardSpec(b.squares_x, b.squares_y, b.square_mm, b.marker_mm, b.dictionary)
        return spec.with_square(self.m.calibration.measured_square_mm)

    # ------------------------------------------------------------------ the camera and the bed
    def _park(self) -> None:
        """The bed out at the photo position, through sortering_pnp's motion (it must be homed)."""
        with self.r._lock:  # noqa: SLF001 - the runner's motion lock
            self.r.motion.preflight(home=False)
            self.r.motion.photo_position()

    def _grab(self) -> tuple[np.ndarray, dict]:
        grab = getattr(self.r.vision, "grab", None)
        if grab is None:
            raise ValueError("this camera cannot take calibration photos")
        with self.r._lock:  # noqa: SLF001
            return grab()

    @staticmethod
    def _save(out: Path, name: str, img: np.ndarray, md: dict, extra: dict) -> Path:
        from sortering_pnp.camera import save_capture

        return save_capture(out, name, img, md, extra)

    # ------------------------------------------------------------------ calibration
    def board_pdf(self) -> bytes:
        from sortering_pnp.calib.board import write_board_pdf

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "board.pdf"
            write_board_pdf(self.spec(), p)
            return p.read_bytes()

    def lock_camera(self) -> dict[str, Any]:
        """Freeze exposure, gain, white balance and focus (camlock.py), kept with the
        machine values; every photo after this uses them."""
        lock = getattr(self.r.vision, "lock_camera", None)
        if lock is None:
            raise ValueError("this camera cannot be locked")
        self._park()
        out = self.root / "camera_settings.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with self.r._lock:  # noqa: SLF001
            settings = lock(out, self.m.calibration.lens_distance_mm)
        self.r.remember_machine({"camera_settings": str(out)})
        self.m.camera.settings = str(out)
        self.r.note(
            f"camera locked: exposure {settings.get('ExposureTime')} us, "
            f"lens {settings.get('LensPosition', 0):.2f} dioptre"
        )
        return self.state()

    def take_view(self) -> dict[str, Any]:
        """One lens photo: the board held somewhere in the frame, tilted a little."""
        from sortering_pnp.calib.board import detect

        d = self._dir() / "lens"
        if not list(d.glob("view_*.png")):
            self._park()  # once: the board is held by hand, nothing moves after that
        img, md = self._grab()
        det = detect(img, self.spec())
        n = det.n if det else 0
        i = len(list(d.glob("view_*.png")))
        self._save(d, f"view_{i:03d}", img, md, {"corners": n})
        cov = self._coverage(img.shape[1], img.shape[0])
        if det is not None:
            cov.add(det.corners)
            write_json(d / "coverage.json", {"grid": cov.grid.tolist()})
        return {**self.state(), "last": {"view": i, "corners": n}}

    def _coverage(self, w: int, h: int) -> Any:
        from sortering_pnp.camera import Coverage

        cov = Coverage((w, h))
        p = self._dir() / "lens" / "coverage.json"
        if p.is_file():
            cov.grid = np.asarray(read_json(p)["grid"], dtype=int)
        return cov

    def solve_lens(self) -> dict[str, Any]:
        from sortering_pnp.calib import intrinsics

        files = sorted(str(f) for f in (self._dir() / "lens").glob("view_*.png"))
        if len(files) < 4:
            raise ValueError(f"{len(files)} lens photo(s): take 25-40 first")
        res = intrinsics.calibrate(files, self.spec())
        write_json(self._dir() / "lens.json", res.to_json())
        used = sum(v.used for v in res.views)
        self.r.note(f"lens solved: {used}/{len(res.views)} photos, error {res.rms:.2f} px")
        return {
            **self.state(),
            "rejected": [f"{Path(v.file).name}: {v.reason}" for v in res.views if not v.used],
        }

    def flat_board(self) -> dict[str, Any]:
        """The board flat in the tray: where the camera stands over the plate. Saves the
        calibration to the Files tab and asks for the camera-to-printer step again."""
        import cv2

        from sortering_pnp.calib import extrinsic
        from sortering_pnp.calib.geometry import Camera

        lens_p = self._dir() / "lens.json"
        if not lens_p.is_file():
            raise ValueError("solve the lens first")
        lens = read_json(lens_p)
        self._park()
        img, md = self._grab()
        self._save(self._dir(), "board_flat", img, md, {})
        c = self.m.calibration
        ext = extrinsic.solve(
            img,
            self.spec(),
            Camera.from_calibration(lens),
            c.board_thickness_mm,
            {"size_mm": list(c.area_mm), "margin_mm": c.margin_mm, "origin_mm": None},
        )
        ext["working_area_mm"]["source"] = "centred on the board in the tray"
        calib = {
            **lens,
            "extrinsic": ext,
            "roi": ext["roi"],
            "px_per_mm": ext["px_per_mm"],
            "plate_homography": ext["plate_homography"],
            "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "board": {**self.spec().to_dict()},
            "made_by": "sortering-pnp Calibrate tab",
        }
        _ = cv2  # (OpenCV is needed by the solver; imported to fail early without it)
        self.r.add_file(self._cam_key(), json.dumps(calib, indent=2).encode(), "calibration.json")
        self.r.calibration_replaced()
        self.r.note(
            f"camera calibrated: {ext['camera_height_mm']:.1f} mm above the plate, "
            f"{ext['px_per_mm']:.2f} px/mm, error {ext['reprojection_rms_px']:.2f} px"
        )
        return {**self.state(), "result": {k: ext[k] for k in ("camera_height_mm", "px_per_mm")}}

    def reset(self) -> dict[str, Any]:
        shutil.rmtree(self._dir(), ignore_errors=True)
        return self.state()

    def state(self) -> dict[str, Any]:
        d = self._dir()
        views = sorted((d / "lens").glob("view_*.png"))
        corners = []
        for v in views:
            side = v.with_suffix(".json")
            corners.append(read_json(side).get("corners", 0) if side.is_file() else 0)
        cov = None
        if (d / "lens" / "coverage.json").is_file():
            g = np.asarray(read_json(d / "lens" / "coverage.json")["grid"])
            cov = round(float((g > 0).mean()), 3)
        lens = read_json(d / "lens.json") if (d / "lens.json").is_file() else None
        c = self.m.calibration
        return {
            "camera": self._cam_key(),
            "board": {**self.spec().to_dict(), "size_mm": list(self.spec().size_mm)},
            "measured_square_mm": c.measured_square_mm,
            "board_thickness_mm": c.board_thickness_mm,
            "locked": bool(self.m.camera.settings),
            "views": len(views),
            "views_with_board": sum(1 for n in corners if n >= 12),
            "coverage": cov,
            "lens": None
            if lens is None
            else {"rms_px": lens.get("rms_px"), "model": lens.get("distortion_model")},
        }

    # ------------------------------------------------------------------ real photos
    @property
    def sessions_root(self) -> Path:
        p = self.m.path(self.m.paths.sessions)
        assert p is not None
        return p

    def sessions(self) -> list[dict[str, Any]]:
        out = []
        if self.sessions_root.is_dir():
            for d in sorted(self.sessions_root.iterdir()):
                meta = d / "session.json"
                if meta.is_file():
                    m = read_json(meta)
                    out.append(
                        {
                            "id": d.name,
                            "created": m.get("created", ""),
                            "operator": m.get("operator", ""),
                            "notes": m.get("notes", ""),
                            "photos": len(list((d / "images").glob("img_*.png"))),
                        }
                    )
        return out

    def new_session(self, name: str, operator: str = "", notes: str = "") -> dict[str, Any]:
        from sortering_pnp.session import new_session

        from .files import find

        settings = self.m.path(self.m.camera.settings)
        new_session(
            self.sessions_root,
            name or None,
            operator,
            notes,
            settings if settings and settings.is_file() else None,
            find(self.m, self._cam_key())[0],
        )
        return {"sessions": self.sessions()}

    def session_photo(self, name: str, note: str = "") -> dict[str, Any]:
        d = self.sessions_root / name
        if not (d / "session.json").is_file():
            raise ValueError(f"no session {name!r}")
        self._park()
        img, md = self._grab()
        n = len(list((d / "images").glob("img_*.png"))) + 1
        meta = read_json(d / "session.json")
        self._save(
            d / "images",
            f"img_{n:04d}",
            img,
            md,
            {
                "session": name,
                "settings_file": self.m.camera.settings or "",
                "notes": note,
                "operator": meta.get("operator", ""),
            },
        )
        self.r.note(f"photo img_{n:04d} saved in session {name}")
        return {"sessions": self.sessions(), "last": f"img_{n:04d}"}

    def session_zip(self, name: str) -> Path:
        """The session folder as a zip on disk (photos are big: it is streamed, not held)."""
        d = self.sessions_root / name
        if not (d / "session.json").is_file():
            raise ValueError(f"no session {name!r}")
        out = self.root / f"{name}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:  # PNG is compressed already
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    z.write(f, f"{name}/{f.relative_to(d).as_posix()}")
        return out
