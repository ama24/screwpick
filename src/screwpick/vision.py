"""Photo -> parts and bin markers, with the screwsynth model and the camera calibration.

The model runs on the calibrated region of interest exactly as it was trained (same scale), and
the sorter then keeps only the parts inside the working area: parts inside bins are ignored.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .config import Machine
from .markers import MarkerPose, detect, marker_pose
from .parts import Part

log = logging.getLogger("screwpick.vision")


@dataclass
class Shot:
    name: str
    parts: list[Part]
    markers: list[MarkerPose] = field(default_factory=list)  # plate frame
    photo: Path | None = None
    vis: Path | None = None  # photo with the outlines drawn
    result: dict[str, Any] = field(default_factory=dict)


class Vision(Protocol):
    def capture(self, name: str, marker_height: Callable[[int], float]) -> Shot:
        """Take a photo, find parts and markers; `marker_height(id)` = plate-frame height of a
        marker (bins differ in height)."""

    def close(self) -> None: ...


_M = re.compile(r"_M(\d+(?:\.\d+)?)")


def top_height_mm(cls: str, screw_head_radius: float | None) -> float:
    """Highest point of a lying part above the plate, for the picker's descent.

    Screws and standoffs: from the measurement geometry (the head or hex body lying on its side).
    Other parts: a rule of thumb from the thread size; the spring-loaded cup absorbs the rest."""
    if screw_head_radius is not None:
        return 2.0 * screw_head_radius
    m = _M.search(cls)
    d = float(m.group(1)) if m else 4.0
    if cls.startswith("BRACKET_"):
        return float(cls.split("_")[1][:2])  # lying on a leg: about the profile size
    if cls.startswith("TNUT_"):
        return 4.0
    if cls.startswith(("DIN125", "DIN9021", "DIN127", "ISO7089", "ISO7093")):
        return max(0.5, 0.2 * d)  # washers
    if cls.startswith("UNKNOWN"):
        return 3.0
    return 0.8 * d  # nuts and the like, lying flat


def parts_from_result(
    result: dict[str, Any],
    to_plate: Callable[[np.ndarray, float], np.ndarray],
    head_radius: Callable[[str], float | None],
) -> list[Part]:
    """screwsynth-infer output -> Parts (outline in plate mm at the part's mid height)."""
    out = []
    for o in result.get("objects", []):
        cls = str(o["class"])
        hr = head_radius(cls)
        top = top_height_mm(cls, hr)
        poly_px = np.asarray(o.get("polygon") or [], dtype=float).reshape(-1, 2)
        poly = to_plate(poly_px, top / 2)[:, :2] if len(poly_px) else np.zeros((0, 2))
        length = (o.get("length") or {}).get("length_mm")
        out.append(
            Part(
                id=int(o["id"]),
                cls=cls,
                score=float(o.get("score", 0.0)),
                plate_xy=(float(o["position_mm"][0]), float(o["position_mm"][1])),
                plate_angle=float(o.get("orientation_deg", 0.0)),
                polygon=poly,
                top_mm=top,
                length_mm=None if length is None else float(length),
                measurable=bool(o.get("measurable", False)),
                unknown=bool(o.get("unknown", False)),
            )
        )
    return out


class CameraVision:
    """The real thing: Pi camera + screwsynth's inference pipeline."""

    def __init__(self, m: Machine, out_dir: Path) -> None:
        import cv2  # noqa: F401 - fail early when OpenCV is missing
        from screwsynth.calibration.geometry import Camera, nominal_from_config
        from screwsynth.common.config import load_config
        from screwsynth.common.io import read_json
        from screwsynth.common.paths import resolve
        from screwsynth.measure.geometry import Geometry
        from screwsynth.measure.infer import Pipeline, Settings

        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        cfg = load_config(m.path(m.paths.screwsynth_config))
        calib_path = m.path(m.paths.calibration) or resolve(
            Path(cfg.get("paths.calibration_dir", "data/calibration")) / "calibration.json"
        )
        calibration = read_json(calib_path) if calib_path.is_file() else None
        if calibration is None:
            log.warning("no camera calibration (%s): using the nominal camera", calib_path)
        nominal = nominal_from_config(cfg)
        self.cam_full = Camera.from_calibration(calibration) if calibration else nominal
        model = m.path(m.paths.model) or _find_model(resolve("models"))
        if model is None:
            raise FileNotFoundError(
                "no model: set paths.model in machine.yaml or copy one into models/"
            )
        geometry = Geometry.from_config(cfg)
        self.pipe = Pipeline(
            model,
            m.path(m.paths.stage2),
            geometry,
            Settings.from_config(cfg.to_dict()),
            calibration,
            resolve(cfg.get("paths.classes", "configs/classes.yaml")),
            nominal=nominal,
        )
        self._head_r = {name: g.head_radius for name, g in geometry.classes.items()}
        self.settings = m.path(m.camera.settings)
        self.cam = None
        if self.settings:
            from screwrig.camera import Camera as RigCamera

            self.cam = RigCamera(read_json(self.settings))

    def _snap(self, path: Path) -> None:
        import cv2

        if self.cam is not None:
            img, _ = self.cam.grab()
            if not cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise OSError(f"could not write {path}")
            return
        cmd = ["rpicam-still", "-n", "-t", "500", "--width", "4608", "--height", "2592"]
        subprocess.run([*cmd, "-q", "95", "-o", str(path)], check=True, capture_output=True)

    def capture(self, name: str, marker_height: Callable[[int], float]) -> Shot:
        import cv2
        from screwsynth.measure.infer import draw

        photo = self.out / f"{name}.jpg"
        self._snap(photo)
        result, scene, _ = self.pipe.run(photo)
        parts = parts_from_result(
            result,
            lambda px, h: scene.cam.pixel_to_plate(px, h),
            self._head_r.get,
        )
        full = cv2.imread(str(photo), cv2.IMREAD_GRAYSCALE)
        markers = [
            marker_pose(
                s, lambda px, mid=s.marker: self.cam_full.pixel_to_plate(px, marker_height(mid))
            )
            for s in detect(full)
        ]
        vis = self.out / f"{name}_vis.jpg"
        cv2.imwrite(str(vis), draw(scene, result, max_side=1600), [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Shot(name, parts, markers, photo, vis, result)

    def close(self) -> None:
        if self.cam is not None:
            self.cam.close()


def _find_model(models_dir: Path) -> Path | None:
    """The newest model in models/: an NCNN folder, .onnx or .pt (as screwrig detect does)."""
    if not models_dir.is_dir():
        return None
    found = [p for p in models_dir.rglob("*_ncnn_model") if p.is_dir()]
    found += [p for p in models_dir.rglob("*") if p.suffix in (".onnx", ".pt")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None
