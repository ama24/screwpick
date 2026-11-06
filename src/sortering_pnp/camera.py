"""The Pi Camera 3 with locked settings, and helpers to check and save its frames.

Picamera2 is only imported when a Camera is opened, so everything else here is testable on the PC.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("sortering_pnp.camera")

FULL_RES = (4608, 2592)
LIVE_SIZE = (1280, 720)  # the live picture on the page (same view, scaled: 16:9 like FULL_RES)
# tolerances when checking that the camera really runs with the locked settings
TOLERANCE = {"ExposureTime": 0.01, "AnalogueGain": 0.02, "ColourGains": 0.01, "LensPosition": 0.02}


def focus_metric(gray: np.ndarray, centre_fraction: float = 0.4) -> float:
    """Variance of the Laplacian on the centre region (higher = sharper)."""
    h, w = gray.shape[:2]
    ch, cw = int(h * centre_fraction), int(w * centre_fraction)
    crop = gray[(h - ch) // 2 : (h + ch) // 2, (w - cw) // 2 : (w + cw) // 2].astype(np.float64)
    lap = (
        -4 * crop[1:-1, 1:-1] + crop[:-2, 1:-1] + crop[2:, 1:-1] + crop[1:-1, :-2] + crop[1:-1, 2:]
    )
    return float(lap.var())


def best_focus(positions: list[float], scores: list[float]) -> float:
    """Peak of the focus curve with a parabolic fit around the best sample."""
    i = int(np.argmax(scores))
    if 0 < i < len(scores) - 1:
        x = np.array(positions[i - 1 : i + 2])
        y = np.array(scores[i - 1 : i + 2])
        a, b, _ = np.polyfit(x, y, 2)
        if a < 0:
            return float(np.clip(-b / (2 * a), x[0], x[-1]))
    return float(positions[i])


def settings_mismatch(locked: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    """Differences between the locked settings and the metadata of an actual frame."""
    problems = []
    for key, tol in TOLERANCE.items():
        if key not in locked:
            continue
        want, got = locked[key], metadata.get(key)
        if got is None:
            problems.append(f"{key}: missing in frame metadata")
            continue
        want_a, got_a = (
            np.atleast_1d(np.asarray(want, float)),
            np.atleast_1d(np.asarray(got, float)),
        )
        rel = np.abs(got_a - want_a) / np.maximum(np.abs(want_a), 1e-9)
        if np.any(rel > tol):
            problems.append(f"{key}: locked {want}, camera reports {got}")
    if (
        locked.get("size")
        and metadata.get("size")
        and list(locked["size"]) != list(metadata["size"])
    ):
        problems.append(f"size: locked {locked['size']}, got {metadata['size']}")
    return problems


def controls_from_settings(s: dict[str, Any]) -> dict[str, Any]:
    """Picamera2 controls that freeze exposure, gain, white balance and focus."""
    c = {
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": int(s["ExposureTime"]),
        "AnalogueGain": float(s["AnalogueGain"]),
        "ColourGains": tuple(float(g) for g in s["ColourGains"]),
        "AfMode": 0,  # controls.AfModeEnum.Manual
        "LensPosition": float(s["LensPosition"]),
    }
    for key in ("Sharpness", "Contrast", "Saturation", "Brightness", "NoiseReductionMode"):
        if key in s:
            c[key] = s[key]
    return c


class Coverage:
    """Where board corners have been seen across the frame (grid of counts)."""

    def __init__(self, size: tuple[int, int] = FULL_RES, cells: tuple[int, int] = (16, 9)) -> None:
        self.size, self.cells = size, cells
        self.grid = np.zeros((cells[1], cells[0]), dtype=int)

    def add(self, corners_px: np.ndarray) -> None:
        w, h = self.size
        cx = np.clip((corners_px[:, 0] / w * self.cells[0]).astype(int), 0, self.cells[0] - 1)
        cy = np.clip((corners_px[:, 1] / h * self.cells[1]).astype(int), 0, self.cells[1] - 1)
        np.add.at(self.grid, (cy, cx), 1)

    @property
    def fraction(self) -> float:
        return float((self.grid > 0).mean())

    def ascii(self) -> str:
        chars = " .:-=+*#"
        top = max(int(self.grid.max()), 1)
        return "\n".join(
            "".join(chars[min(7, int(v * 7 / top))] if v else "_" for v in row) for row in self.grid
        )

    def image(self, scale: int = 40) -> np.ndarray:
        g = (np.clip(self.grid / max(self.grid.max(), 1), 0, 1) * 255).astype(np.uint8)
        return np.kron(g, np.ones((scale, scale), dtype=np.uint8))


def save_capture(
    out_dir: str | os.PathLike[str],
    name: str,
    image_bgr: np.ndarray,
    metadata: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> Path:
    """Lossless PNG + JSON sidecar with the actual frame metadata."""
    import cv2

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    if not cv2.imwrite(str(path), image_bgr):
        raise OSError(f"could not write {path}")
    sidecar = {
        "file": path.name,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "metadata": {k: _jsonable(v) for k, v in metadata.items()},
        **(extra or {}),
    }
    path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return path


def _jsonable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    if hasattr(v, "tolist"):
        return v.tolist()
    return str(v)


def rpicam_still(hflip: bool = False, vflip: bool = False) -> list[str]:
    """The rpicam-still command for one full-resolution photo (without Picamera2)."""
    cmd = ["rpicam-still", "-n", "-t", "500", "--width", str(FULL_RES[0])]
    cmd += ["--height", str(FULL_RES[1])]
    return cmd + (["--hflip"] if hflip else []) + (["--vflip"] if vflip else [])


class CameraError(RuntimeError):
    """The camera cannot be used as asked (shown on the page; never ends the program)."""


class Camera:
    """The Pi camera, kept open while the program runs. Locked (`settings` from camlock: the same
    exposure, colours and focus for every photo) or, before the lock, with the camera's own
    automatic settings (only for the live picture and the lock itself). Two streams: "main" at
    full resolution for photos, "lores" (LIVE_SIZE) for the live picture on the page."""

    def __init__(
        self, settings: dict | None = None, hflip: bool = False, vflip: bool = False
    ) -> None:
        from picamera2 import Picamera2

        self.settings = dict(settings or {})
        extra: dict[str, Any] = {}
        if hflip or vflip:  # done by the camera: every stream comes out turned
            from libcamera import Transform

            extra["transform"] = Transform(hflip=int(hflip), vflip=int(vflip))
        self.locked = bool(self.settings)
        self.picam2 = Picamera2()
        try:
            mode = self.settings.get("sensor_mode") or {}
            cfg = self.picam2.create_still_configuration(
                main={"size": tuple(self.settings.get("size", FULL_RES)), "format": "RGB888"},
                lores={"size": LIVE_SIZE},
                raw={} if mode else None,
                sensor={"output_size": tuple(mode["size"]), "bit_depth": mode["bit_depth"]}
                if mode
                else {},
                buffer_count=2,
                **extra,
            )
            self.picam2.configure(cfg)
            if self.locked:
                self.picam2.set_controls(controls_from_settings(self.settings))
            else:
                auto: dict[str, Any] = {"AeEnable": True, "AwbEnable": True}
                if "AfMode" in self.picam2.camera_controls:
                    auto["AfMode"] = 2  # controls.AfModeEnum.Continuous: sharp while unlocked
                self.picam2.set_controls(auto)
            self.picam2.start()
            time.sleep(1.5)
            if self.locked:
                self.verify()
        except Exception:
            self.close()
            raise

    def verify(self) -> None:
        md = dict(self.picam2.capture_metadata())
        md["size"] = list(self.settings.get("size", FULL_RES))
        problems = settings_mismatch(self.settings, md)
        if problems:
            raise CameraError(
                "the camera does not run with the locked settings (Calibrate the camera again): "
                + "; ".join(problems)
            )

    def grab(self, dng_path: Path | None = None) -> tuple[np.ndarray, dict]:
        request = self.picam2.capture_request()
        try:
            img = request.make_array("main")
            md = request.get_metadata()
            if dng_path is not None:
                request.save_dng(str(dng_path))
        finally:
            request.release()
        problems = settings_mismatch(self.settings, md)
        if problems:
            raise CameraError("settings drifted during capture: " + "; ".join(problems))
        return img, md

    def preview(self) -> np.ndarray:
        """One frame of the live picture (LIVE_SIZE, BGR)."""
        import cv2

        yuv = self.picam2.capture_array("lores")
        w, h = LIVE_SIZE
        return cv2.cvtColor(np.ascontiguousarray(yuv[: h * 3 // 2, :w]), cv2.COLOR_YUV2BGR_I420)

    def close(self) -> None:
        """Stop and release the camera, so another Picamera2 (the lock) can open it."""
        try:
            self.picam2.stop()
        finally:
            self.picam2.close()
