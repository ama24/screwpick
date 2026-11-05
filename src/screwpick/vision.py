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

from .analysis import Analyzer, ModelChoice
from .config import Machine
from .grip import Shape, axis_grip, outline_grip
from .markers import MarkerPose, detect, marker_pose
from .models import ModelStore
from .parts import Part
from .trayview import WorkArea, paint_outside, parse_colour

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
) -> list[Part]:
    """screwsynth-infer output -> Parts (outline in plate mm at the part's mid height), with
    grip point, centre of mass and long axis (grip.py)."""
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
            g = axis_grip(dict(zip(("head_top", "bearing", "tip"), pts[:, :2], strict=True)), sh)
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


def shapes_from_catalogue(cfg: Any, geometry: Any) -> dict[str, Shape]:
    """Per class: thread size, head radius and head height, for the centre of mass."""
    from screwsynth.catalogue.schema import LENGTH_KINDS, catalogue_from_config

    out: dict[str, Shape] = {}
    for p in catalogue_from_config(cfg).enabled_parts:
        if p.kind not in LENGTH_KINDS or p.class_name in out:
            continue
        g = geometry.classes.get(p.class_name)
        dims = p.dims
        d = float(dims.get("d", g.d if g else 4.0))
        headless = p.kind == "standoff" or p.shape in ("set_screw", "threaded_rod")
        out[p.class_name] = Shape(
            d=d,
            head_r=0.0 if headless else float(g.head_radius if g else dims.get("dk", d) / 2),
            head_k=0.0 if headless else float(dims.get("k", 0.0)),
            socket=p.shape in ("socket_cap", "button", "countersunk"),
        )
    return out


class CameraVision:
    """The real thing: Pi camera + the active models from the model store + screwsynth's
    calibration and length measurement. Models can be swapped while idle (use_models)."""

    def __init__(self, m: Machine, out_dir: Path, store: ModelStore | None = None) -> None:
        import cv2  # noqa: F401 - fail early when OpenCV is missing
        from screwsynth.common.io import read_json

        self.m = m
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.analyzer: Analyzer | None = None
        self.model_error: str | None = None
        self.reload_setup()
        cam_settings = m.path(m.camera.settings)
        self.cam = None
        if cam_settings:
            from screwrig.camera import Camera as RigCamera

            self.cam = RigCamera(read_json(cam_settings))

    def reload_setup(self) -> None:
        """Settings, camera calibration and class list (the Files tab's copies first, see
        files.py), then the models again with them."""
        from screwsynth.calibration.geometry import Camera, nominal_from_config
        from screwsynth.common.config import load_config
        from screwsynth.common.io import read_json
        from screwsynth.measure.geometry import Geometry
        from screwsynth.measure.infer import Settings

        from .files import find

        m = self.m
        cfg = load_config(find(m, "settings")[0])
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
        self.geometry = Geometry.from_config(cfg)
        self.settings = Settings.from_config(cfg.to_dict())
        self.classes_yaml = find(m, "classes")[0]
        self._head_r = {name: g.head_radius for name, g in self.geometry.classes.items()}
        try:
            self._shapes = shapes_from_catalogue(cfg, self.geometry)
        except Exception as exc:  # noqa: BLE001 - grip falls back to the outline
            log.warning("no head sizes for the centre of mass: %s", exc)
            self._shapes = {}
        try:
            self.use_models(active_choice(m, self.store))
        except (FileNotFoundError, ValueError) as exc:
            self.model_error = str(exc)
            log.error("no usable model: %s", exc)

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
        from screwsynth.measure.infer import load_scene

        return load_scene(photo, self.calibration, self.nominal)

    def _snap(self, path: Path) -> None:
        import cv2

        if self.cam is not None:
            img, _ = self.cam.grab()
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
        if self.analyzer is None:
            raise FileNotFoundError(
                f"no model: upload one in the Models tab ({self.model_error or 'none loaded'})"
            )
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
        import cv2
        from screwsynth.measure.infer import draw

        assert self.analyzer is not None
        scene = self._scene(photo)
        if work is not None and self.m.camera.mask:
            scene = self._painted(scene, work)
        result = self.analyzer.analyse(scene, str(photo))
        parts = parts_from_result(
            result,
            lambda px, h: scene.cam.pixel_to_plate(px, h),
            self._head_r.get,
            self._shapes.get,
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

    def test_models(self, choice: ModelChoice, photo: Path, out_name: str) -> dict[str, Any]:
        """Run other models on an existing photo, without switching to them."""
        import cv2
        from screwsynth.measure.infer import draw

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
        if self.cam is not None:
            self.cam.close()


def find_model(models_dir: Path) -> Path | None:
    """The newest model in a models/ folder: an NCNN folder, .onnx or .pt (as screwrig detect)."""
    if not models_dir.is_dir():
        return None
    found = [p for p in models_dir.rglob("*_ncnn_model") if p.is_dir()]
    found += [p for p in models_dir.rglob("*") if p.suffix in (".onnx", ".pt")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def active_choice(m: Machine, store: ModelStore | None) -> ModelChoice:
    """The models to use: the active ones in the store; without one, paths.model in machine.yaml
    or the newest model in screwsynth's models/ folder."""
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
        if path is None:
            from screwsynth.common.paths import resolve

            path = find_model(resolve("models"))
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
