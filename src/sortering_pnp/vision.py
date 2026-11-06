"""Photo -> parts and bin markers, with the models and the camera calibration.

The model runs on the calibrated region of interest exactly as it was trained (same scale), and
the sorter then keeps only the parts inside the working area: parts inside bins are ignored.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .analysis import Analyzer, ModelChoice
from .config import Machine
from .grip import Shape, axis_grip, head_grip, outline_grip
from .markers import MarkerPose, detect, marker_pose
from .models import ModelStore
from .parts import Part
from .trayview import WorkArea, paint_outside, parse_colour

log = logging.getLogger("sortering_pnp.vision")


@dataclass
class Shot:
    name: str
    parts: list[Part]
    markers: list[MarkerPose] = field(default_factory=list)  # plate frame
    photo: Path | None = None
    vis: Path | None = None  # photo with the outlines drawn
    result: dict[str, Any] = field(default_factory=dict)
    view: Path | None = None  # the photo without drawings (the page draws the outlines)
    view_h: list[list[float]] | None = None  # plate mm (z = 0) -> view pixels


class Vision(Protocol):
    def capture(
        self, name: str, marker_height: Callable[[int], float], work: WorkArea | None = None
    ) -> Shot:
        """Take a photo, find parts and markers; `marker_height(id)` = plate-frame height of a
        marker (bins differ in height); `work`: the model sees only the tray's inside."""

    def snap(self, name: str) -> Any:
        """Only take the photo (fast); `analyse` it later, e.g. while the machine moves."""

    def analyse(
        self,
        photo: Any,
        name: str,
        marker_height: Callable[[int], float],
        work: WorkArea | None = None,
    ) -> Shot: ...

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


def _keypoints_px(o: dict[str, Any]) -> dict[str, list[float]] | None:
    k = o.get("keypoints") or {}
    kp = k.get("keypoints") or k.get("classical")
    if kp and all(n in kp for n in ("head_top", "bearing", "tip")):
        return kp
    return None


def parts_from_result(
    result: dict[str, Any],
    to_plate: Callable[[np.ndarray, float], np.ndarray],
    head_radius: Callable[[str], float | None],
    shape: Callable[[str], Shape | None] | None = None,
    aim: str = "centre",
) -> list[Part]:
    """Inference output (measure/infer.py) -> Parts (outline in plate mm at the part's mid height), with
    grip point, centre of mass and long axis (grip.py). `aim`: "centre" (tweezers: the centre
    of gravity, on the shank) or "head" (suction cup, magnet: the screw's head)."""
    out = []
    for o in result.get("objects", []):
        cls = str(o["class"])
        hr = head_radius(cls)
        top = top_height_mm(cls, hr)
        poly_px = np.asarray(o.get("polygon") or [], dtype=float).reshape(-1, 2)
        poly = to_plate(poly_px, top / 2)[:, :2] if len(poly_px) else np.zeros((0, 2))
        length = (o.get("length") or {}).get("length_mm")
        g = None
        sh = shape(cls) if shape else None
        kp = _keypoints_px(o)
        if sh is not None and kp is not None:  # the axis from the keypoints, lying at d/2
            pts = to_plate(np.array([kp[n] for n in ("head_top", "bearing", "tip")]), sh.d / 2)
            kpm = dict(zip(("head_top", "bearing", "tip"), pts[:, :2], strict=True))
            g = head_grip(kpm, sh) if aim == "head" else axis_grip(kpm, sh)
        if g is None and len(poly):
            g = outline_grip(poly)
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
                has_length=hr is not None,
                grip_plate=g.point if g else None,
                com_plate=g.com if g else None,
                grip_method=g.method if g else "centre",
                grip_height_mm=sh.d / 2 if (g is not None and g.method == "axis" and sh) else None,
            )
        )
        if g is not None:
            out[-1].plate_angle = g.axis_deg  # the axis from keypoints / area moments
    return out


def shapes_from_parts(parts: Mapping[str, Any]) -> dict[str, Shape]:
    """Per class (parts.json): thread size, head radius and head height, for the centre of
    mass."""
    out: dict[str, Shape] = {}
    for name, c in parts.get("classes", {}).items():
        headless = bool(c.get("headless"))
        out[name] = Shape(
            d=float(c["d"]),
            head_r=0.0 if headless else float(c["head_radius"]),
            head_k=0.0 if headless else float(c.get("head_height", 0.0)),
            socket=bool(c.get("socket")),
        )
    return out


class CameraVision:
    """The real thing: Pi camera + the active models from the model store + the camera
    calibration and length measurement. Models can be swapped while idle (reload_setup)."""

    def __init__(self, m: Machine, out_dir: Path, store: ModelStore | None = None) -> None:
        import cv2  # noqa: F401 - fail early when OpenCV is missing

        self.m = m
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.analyzer: Analyzer | None = None
        self.model_error: str | None = None
        self.reload_setup()
        # the Pi camera: opened when first needed and kept open (photos and the live picture)
        self.cam: Any = None
        self._cam_lock = threading.RLock()  # one user of the camera at a time
        self._no_picamera = False  # Picamera2 missing: photos through rpicam-still, no live picture
        self._latest: np.ndarray | None = None  # the newest live frame
        self._latest_at = 0.0
        self._wanted = 0.0  # when the live picture was last asked for
        self._reader: threading.Thread | None = None
        self.live_error: str | None = None

    def reload_setup(self, strict: bool = False) -> None:
        """Vision settings, camera calibration, parts table and class list (the active model's
        and the Files tab's copies first, see files.py), then the models again with them.
        strict: a model that cannot be loaded raises (else it is logged and shown)."""
        from sortering_pnp.calib.geometry import Camera, nominal_from_config
        from sortering_pnp.io import read_json, read_yaml
        from sortering_pnp.measure.geometry import Geometry
        from sortering_pnp.measure.infer import Settings

        from .files import find

        m = self.m
        vision_path, _ = find(m, "vision")
        cfg = (read_yaml(vision_path) or {}) if vision_path is not None else {}
        key = "head_calibration" if m.camera.source == "head" else "calibration"
        calib_path, source = find(m, key)
        self.calibration = read_json(calib_path) if calib_path is not None else None
        if self.calibration is None:
            log.warning("no camera calibration (Files tab): using the nominal camera")
        else:
            log.info("camera calibration: %s (%s)", calib_path, source)
        self.nominal = nominal_from_config(cfg)
        self.cam_full = (
            Camera.from_calibration(self.calibration) if self.calibration else self.nominal
        )
        self.settings = Settings.from_config(cfg)
        parts_path, source = find(m, "parts")
        parts = read_json(parts_path) if parts_path is not None else None
        try:
            self.geometry = Geometry.from_parts(parts)
            log.info("parts table: %s (%s)", parts_path, source)
        except ValueError as exc:
            log.warning("no parts table, lengths are not measured: %s", exc)
            self.geometry, parts = Geometry({}), {}
        self.classes_yaml = find(m, "classes")[0]
        self._head_r = {name: g.head_radius for name, g in self.geometry.classes.items()}
        self._shapes = shapes_from_parts(parts)
        try:
            self.use_models(active_choice(m, self.store))
        except (FileNotFoundError, ValueError) as exc:
            self.model_error = str(exc)
            log.error("no usable model: %s", exc)
            if strict:
                raise

    def load(self, choice: ModelChoice) -> Analyzer:
        return Analyzer(
            choice, self.geometry, self.settings, self.classes_yaml, self.m.models.classifier_conf
        )

    def use_models(self, choice: ModelChoice) -> dict[str, Any]:
        """Load and switch to these models (the old ones stay in use if loading fails)."""
        self.analyzer = self.load(choice)
        self.model_error = None
        log.info("models: %s (%s)", choice.label, self.analyzer.task)
        return self.analyzer.describe()

    def models_info(self) -> dict[str, Any]:
        if self.analyzer is None:
            return {"error": self.model_error or "no model loaded"}
        return self.analyzer.describe()

    def _view(self, scene: Any, name: str, max_side: int = 1600) -> tuple[Path, list, float]:
        """The photo for the page, scaled down, the homography plate mm -> its pixels, and the
        scale (its pixels per photo pixel)."""
        import cv2

        h, w = scene.image.shape[:2]
        k = min(1.0, max_side / max(h, w))
        img = cv2.resize(scene.image, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
        path = self.out / f"{name}_view.jpg"
        cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        g = np.arange(-20.0, 241.0, 20.0)
        plate = np.array([(x, y) for x in g for y in g])
        px = scene.cam.project(np.c_[plate, np.zeros(len(plate))])
        ok = (px[:, 0] > -w) & (px[:, 0] < 2 * w) & (px[:, 1] > -h) & (px[:, 1] < 2 * h)
        H, _ = cv2.findHomography(plate[ok], px[ok])
        return path, (np.diag([k, k, 1.0]) @ H).tolist(), k

    def _painted(self, scene: Any, work: WorkArea) -> Any:
        import dataclasses

        def px(poly: np.ndarray) -> np.ndarray:
            return scene.cam.project(np.c_[poly, np.zeros(len(poly))])

        img = paint_outside(
            scene.image,
            px(work.outline),
            [px(h) for h in work.holes],
            parse_colour(self.m.camera.mask_colour),
        )
        return dataclasses.replace(scene, image=img, gray=None)

    def _scene(self, photo: Path) -> Any:
        from sortering_pnp.measure.infer import load_scene

        return load_scene(photo, self.calibration, self.nominal)

    # ------------------------------------------------------------------ the camera
    def _camera(self) -> Any:
        """The open Pi camera (locked settings when there are some), or None without Picamera2."""
        from sortering_pnp.io import read_json

        if self.cam is None and not self._no_picamera:
            try:
                from sortering_pnp.camera import Camera as RigCamera
            except ImportError:
                self._no_picamera = True
                return None
            path = self.m.path(self.m.camera.settings)
            settings = read_json(path) if path is not None and path.is_file() else None
            try:
                self.cam = RigCamera(settings)
            except ImportError:
                self._no_picamera = True
                log.warning("no Picamera2: photos with rpicam-still, no live picture")
        return self.cam

    def frame(self) -> np.ndarray | None:
        """The newest live frame (camera.LIVE_SIZE, BGR), or None (no camera / not yet). Starts
        the reader, which stops by itself a few seconds after nobody asks any more."""
        self._wanted = time.monotonic()
        if self._reader is None or not self._reader.is_alive():
            self._reader = threading.Thread(target=self._read_live, name="camera-live", daemon=True)
            self._reader.start()
            for _ in range(60):  # the first frame, up to 3 s (opening the camera)
                if self._latest is not None and time.monotonic() - self._latest_at < 5:
                    break
                if self.live_error:
                    break
                time.sleep(0.05)
        if self._latest is None or time.monotonic() - self._latest_at > 5:
            return None
        return self._latest

    def _read_live(self) -> None:
        while time.monotonic() - self._wanted < 5:
            try:
                with self._cam_lock:
                    cam = self._camera()
                    if cam is None:
                        self.live_error = "no live picture: Picamera2 is not installed"
                        return
                    img = cam.preview()
                self._latest, self._latest_at, self.live_error = img, time.monotonic(), None
            except Exception as exc:  # noqa: BLE001 - shown on the page, tried again
                self.live_error = f"camera: {exc}"
                log.warning("live picture: %s", exc)
                time.sleep(1.0)
            time.sleep(0.05)

    def grab(self) -> tuple[np.ndarray, dict[str, Any]]:
        """One full-resolution frame (BGR) and its metadata, for calibration and real photos:
        lossless, with the locked settings when there are some."""
        import cv2

        with self._cam_lock:
            cam = self._camera()
            if cam is not None:
                return cam.grab()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "frame.png"
            cmd = ["rpicam-still", "-n", "-t", "500", "--width", "4608", "--height", "2592"]
            subprocess.run([*cmd, "-e", "png", "-o", str(p)], check=True, capture_output=True)
            img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            raise OSError("rpicam-still gave no picture")
        return img, {}

    def lock_camera(self, out: Path, distance_mm: float) -> dict[str, Any]:
        """The camera lock (camlock.py: exposure, gain, white balance, focus sweep) written to `out`; the
        camera is then reopened with exactly those settings."""
        import argparse

        from sortering_pnp import camlock as lock
        from sortering_pnp.camera import Camera as RigCamera
        from sortering_pnp.io import read_json

        with self._cam_lock:  # the live picture waits meanwhile
            if self.cam is not None:  # one Picamera2 at a time may hold the camera
                self.cam.close()
                self.cam = None
            lock.run(
                argparse.Namespace(
                    out=str(out), settle=3.0, distance_mm=distance_mm, exposure_us=None
                )
            )
            settings = read_json(out)
            self.cam = RigCamera(settings)
        return settings

    def _snap(self, path: Path) -> None:
        import cv2

        with self._cam_lock:
            cam = self._camera()
            img = cam.grab()[0] if cam is not None else None
        if img is not None:
            if not cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise OSError(f"could not write {path}")
            return
        cmd = ["rpicam-still", "-n", "-t", "500", "--width", "4608", "--height", "2592"]
        subprocess.run([*cmd, "-q", "95", "-o", str(path)], check=True, capture_output=True)

    def capture(
        self, name: str, marker_height: Callable[[int], float], work: WorkArea | None = None
    ) -> Shot:
        return self.analyse(self.snap(name), name, marker_height, work)

    def snap(self, name: str) -> Path:
        """Only the photo: no model needed (calibration and Resync read only the codes)."""
        photo = self.out / f"{name}.jpg"
        self._snap(photo)
        return photo

    def analyse(
        self,
        photo: Path,
        name: str,
        marker_height: Callable[[int], float],
        work: WorkArea | None = None,
    ) -> Shot:
        """`work`: the tray's inside (plate frame): the rest is painted over for the model
        (trayview.py); None: the whole photo (e.g. to look into the bins)."""
        import dataclasses

        import cv2

        from sortering_pnp.measure.infer import draw

        scene = self._scene(photo)
        seen = scene  # unpainted: what the page shows
        if work is not None and self.m.camera.mask:
            scene = self._painted(scene, work)
        if self.analyzer is None:  # no model yet: the codes are still read, no parts
            result: dict[str, Any] = {}
            parts: list[Part] = []
        else:
            result = self.analyzer.analyse(scene, str(photo))
            parts = parts_from_result(
                result,
                lambda px, h: scene.cam.pixel_to_plate(px, h),
                self._head_r.get,
                self._shapes.get,
                self.m.picker.kind.aim,
            )
        full = cv2.imread(str(photo), cv2.IMREAD_GRAYSCALE)
        found = detect(full)
        view, view_h, k = self._view(seen, name)
        # the view is the ROI (cropped from a full frame, or the photo itself), scaled by k
        cropped = full.shape[:2] != seen.image.shape[:2]
        ox, oy = (seen.roi["x0"], seen.roi["y0"]) if cropped else (0, 0)
        markers = []
        for s in found:
            cx, cy = np.asarray(s.corners_px).mean(axis=0)
            pose = marker_pose(
                s, lambda px, mid=s.marker: self.cam_full.pixel_to_plate(px, marker_height(mid))
            )
            markers.append(dataclasses.replace(pose, view_px=((cx - ox) * k, (cy - oy) * k)))
        vis = view
        if self.analyzer is not None:
            vis = self.out / f"{name}_vis.jpg"
            img = draw(scene, result, max_side=1600)
            cv2.imwrite(str(vis), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Shot(name, parts, markers, photo, vis, result, view, view_h)

    def model_missing(self) -> str | None:
        """Why the parts cannot be found (no model), or None."""
        if self.analyzer is not None:
            return None
        return f"no model: upload one in the Models tab ({self.model_error or 'none loaded'})"

    def test_models(self, choice: ModelChoice, photo: Path, out_name: str) -> dict[str, Any]:
        """Run other models on an existing photo, without switching to them."""
        import cv2

        from sortering_pnp.measure.infer import draw

        an = self.load(choice)
        scene = self._scene(photo)
        result = an.analyse(scene, str(photo))
        vis = self.out / out_name
        cv2.imwrite(str(vis), draw(scene, result, max_side=1600), [cv2.IMWRITE_JPEG_QUALITY, 85])
        return {
            "image": vis.name,
            "objects": len(result["objects"]),
            "model": an.describe(),
            "seconds": result["timing_s"]["total"],
        }

    def close(self) -> None:
        self._wanted = 0.0  # the live reader stops
        with self._cam_lock:
            if self.cam is not None:
                self.cam.close()
                self.cam = None


def find_model(models_dir: Path) -> Path | None:
    """The newest model in a models/ folder: an NCNN folder, .onnx or .pt."""
    if not models_dir.is_dir():
        return None
    found = [p for p in models_dir.rglob("*_ncnn_model") if p.is_dir()]
    found += [p for p in models_dir.rglob("*") if p.suffix in (".onnx", ".pt")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def active_choice(m: Machine, store: ModelStore | None) -> ModelChoice:
    """The models to use: the active ones in the store; without one, paths.model in machine.yaml
    (a model file, or a folder: its newest model)."""
    s1 = store.active_model("stage1") if store else None
    if m.camera.source == "head" and store is not None:  # its own model, trained at its scale
        head = store.active_model("head")
        if head is None:
            log.warning("head camera: no head camera model chosen, using the stage-1 model")
        s1 = head or s1
    if s1 is not None and store is not None:
        choice = ModelChoice(
            store.weights_path(s1),
            imgsz=s1.imgsz,
            conf=s1.recommended_conf if m.models.use_recommended_conf else None,
            label=s1.name,
        )
    else:
        path = m.path(m.paths.model)
        if path is not None and path.is_dir() and not path.name.endswith("_ncnn_model"):
            path = find_model(path)
        if path is None:
            raise FileNotFoundError("no model: upload one in the Models tab")
        choice = ModelChoice(path, label=path.name)
    for role in ("stage2", "classifier"):
        sm = store.active_model(role) if store else None
        if sm is not None and store is not None:
            setattr(choice, role, store.weights_path(sm))
    if choice.stage2 is None:
        choice.stage2 = m.path(m.paths.stage2)
    return choice
