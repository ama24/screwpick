"""Camera calibration with one button (Calibrate tab): a job that runs every step by itself,
watches the live picture and tells the person what to do next.

1. Park: the bed goes to the photo position, so the camera looks into the tray.
2. Lock: the camera measures the light and the focus once and freezes them (exposure, gain,
   white balance, lens position; about a minute; the tray empty, the lights as when sorting).
   Every later photo then looks the same, which both the lens maths and the model need.
3. Lens: the person holds the printed board under the camera and moves it about. Each time it
   holds still somewhere new (another place, size or tilt) a full-resolution photo is taken by
   itself, until enough of the picture is covered; then the lens (its distortion) is solved.
4. Position: the person lays the board flat in the tray. When it lies still and flat, one photo
   gives where the camera stands over the tray: calibration.json, used at once.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger("sortering_pnp.calibjob")

STILL_FRAMES = 4  # live frames in a row with the board in the same place: it holds still
FLAT_FRAMES = 8  # lying flat in the tray: a little longer
STILL = 0.004  # of the picture's width: the board moved less than this between frames
NEW_PLACE = 0.08  # of the width: a new place for a lens photo (or a new size or tilt)
NEW_SIZE = 0.15
NEW_TILT = 0.03
MIN_CORNERS = 12  # board corners seen for a useful photo
FLAT_TILT_DEG = 8.0  # lying flat: tilted less than this


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Pose:
    """Where the board is in a picture, as fractions of its width: centre, size, and two tilt
    measures (the homography's perspective terms times the board's size: 0 = square on)."""

    x: float
    y: float
    size: float
    tx: float
    ty: float

    def still_as(self, o: Pose) -> bool:
        return math.hypot(self.x - o.x, self.y - o.y) < STILL and abs(self.size / o.size - 1) < 0.02

    def far_from(self, o: Pose) -> bool:
        return (
            math.hypot(self.x - o.x, self.y - o.y) > NEW_PLACE
            or abs(self.size / o.size - 1) > NEW_SIZE
            or math.hypot(self.tx - o.tx, self.ty - o.ty) > NEW_TILT
        )


def pose_of(det: Any, shape: tuple[int, ...]) -> Pose:
    import cv2

    w = shape[1]
    px = det.corners.astype(np.float32).reshape(-1, 2)
    obj = det.object_points[:, :2].astype(np.float32).reshape(-1, 2)
    c = px.mean(axis=0)
    area = cv2.contourArea(cv2.convexHull(px))
    H, _ = cv2.findHomography(obj, px)
    tx = ty = 0.0
    if H is not None and abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]
        span = np.ptp(obj, axis=0)
        tx, ty = float(H[2, 0] * span[0]), float(H[2, 1] * span[1])
    return Pose(float(c[0] / w), float(c[1] / w), math.sqrt(max(area, 1.0)) / w, tx, ty)


class CalibrationJob:
    """The four steps in a thread. `frame()` gives the newest live picture (or None); the photos
    (full resolution), the lock, the lens and the position come from `photos` (photos.Photos)."""

    def __init__(
        self,
        photos: Any,
        frame: Callable[[], np.ndarray | None],
        min_views: int = 20,
        max_views: int = 40,
        coverage: float = 0.7,
        timeout_s: float = 1800.0,
        pause_s: float = 0.05,
    ) -> None:
        self.photos = photos
        self.frame = frame
        self.min_views, self.max_views, self.want_coverage = min_views, max_views, coverage
        self.timeout_s, self.pause_s = timeout_s, pause_s
        self.phase = "starting"
        self.message = "starting…"
        self.views = 0
        self.coverage = 0.0
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.seen: np.ndarray | None = None  # board corners in the newest live frame (overlay)
        self.grid: np.ndarray | None = None  # covered cells of the picture (overlay)
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="calibration", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def running(self) -> bool:
        return self.phase not in ("done", "error", "cancelled")

    def state(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "message": self.message,
            "views": self.views,
            "min_views": self.min_views,
            "coverage": round(self.coverage, 3),
            "want_coverage": self.want_coverage,
            "running": self.running,
            "error": self.error,
            "result": self.result,
        }

    def _say(self, phase: str, message: str) -> None:
        self.phase, self.message = phase, message
        log.info("calibration %s: %s", phase, message)

    # ------------------------------------------------------------------ the steps
    def run(self) -> None:
        try:
            self._say(
                "lock",
                "Locking the camera: tray empty, lights as when sorting. About a minute…",
            )
            self.photos.lock_camera()
            self._check()
            self.photos.reset_lens()
            self._lens()
            self._say("solve", "Solving the lens…")
            res = self.photos.solve_lens()
            rms = (res.get("lens") or {}).get("rms_px")
            if rms is None or rms > 1.5:
                raise ValueError(
                    f"the lens fit is poor ({rms} px): press Calibrate again and move the board "
                    "more slowly, with good light"
                )
            self._flat()
            self._say("position", "Measuring where the camera stands over the tray…")
            res = self.photos.flat_board()
            self.result = res.get("result")
            h = (self.result or {}).get("camera_height_mm")
            self._say(
                "done",
                f"Done: the camera is {h:.0f} mm above the tray. Next: Setup tab, Camera to "
                "printer (the millimetres now start at the board)."
                if h
                else "Done.",
            )
        except Cancelled:
            self._say("cancelled", "Stopped. Press Calibrate to start again.")
        except Exception as exc:  # noqa: BLE001 - shown on the page
            log.exception("calibration failed")
            self.error = str(exc)
            self._say("error", str(exc))
        finally:
            self.seen = None

    def _check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled
        if time.monotonic() - self._t0 > self.timeout_s:
            raise ValueError("the calibration took too long: press Calibrate to start again")

    def _detect(self) -> tuple[np.ndarray, Any] | None:
        """The newest live frame and the board in it (None when there is no frame yet)."""
        from sortering_pnp.calib.board import detect

        self._check()
        img = self.frame()
        if img is None:
            time.sleep(0.2)
            return None
        det = detect(img, self.photos.spec())
        self.seen = None if det is None else det.corners.reshape(-1, 2)
        time.sleep(self.pause_s)
        return img, det

    def _lens(self) -> None:
        self._say(
            "lens",
            "Hold the board under the camera and move it slowly over the whole picture, corners "
            "too, a little tilted now and then. Each time it holds still somewhere new a photo is "
            "taken by itself.",
        )
        taken: list[Pose] = []
        recent: list[Pose] = []
        while True:
            got = self._detect()
            if got is None:
                continue
            img, det = got
            if det is None or det.n < MIN_CORNERS:
                recent = []
                continue
            p = pose_of(det, img.shape)
            recent = [*recent[-(STILL_FRAMES - 1) :], p]
            if len(recent) < STILL_FRAMES or not all(p.still_as(o) for o in recent):
                continue
            if not all(p.far_from(o) for o in taken):
                continue
            if self.photos.lens_photo() >= MIN_CORNERS:  # the full-resolution photo
                taken.append(p)
                self.views = len(taken)
                st = self.photos.state()
                self.coverage = float(st.get("coverage") or 0.0)
                self.grid = self.photos.coverage_grid()
                self.message = (
                    f"{self.views} photo(s), {self.coverage:.0%} of the picture covered. Keep "
                    "moving the board: the corners of the picture count too."
                )
            recent = []
            enough = self.views >= self.min_views and self.coverage >= self.want_coverage
            if enough or self.views >= self.max_views:
                return

    def _flat(self) -> None:
        self._say(
            "flat",
            "Now lay the board flat in the tray, on the TPU sheet where the parts lie, away from "
            "the corner post, and let go.",
        )
        recent: list[Pose] = []
        n_all = self.photos.spec().n_corners
        while True:
            got = self._detect()
            if got is None:
                continue
            img, det = got
            if det is None or det.n < max(MIN_CORNERS, int(0.6 * n_all)):
                recent = []
                continue
            p = pose_of(det, img.shape)
            recent = [*recent[-(FLAT_FRAMES - 1) :], p]
            if len(recent) < FLAT_FRAMES or not all(p.still_as(o) for o in recent):
                continue
            tilt = self.photos.board_tilt_deg()
            if tilt is not None and tilt > FLAT_TILT_DEG:
                self.message = (
                    f"The board is tilted ({tilt:.0f}°): lay it flat on the TPU sheet and let go."
                )
                recent = []
                continue
            return

    # ------------------------------------------------------------------ the live picture
    def draw(self, img: np.ndarray) -> np.ndarray:
        """The covered cells (lens step) and the board corners seen, on a live frame."""
        import cv2

        out = img
        if self.phase == "lens" and self.grid is not None:
            gy, gx = self.grid.shape
            h, w = img.shape[:2]
            tint = out.copy()
            for j in range(gy):
                for i in range(gx):
                    if self.grid[j, i] > 0:
                        x0, y0 = int(i * w / gx), int(j * h / gy)
                        x1, y1 = int((i + 1) * w / gx), int((j + 1) * h / gy)
                        cv2.rectangle(tint, (x0, y0), (x1, y1), (60, 200, 60), -1)
            out = cv2.addWeighted(tint, 0.25, out, 0.75, 0)
        if self.seen is not None and self.running:
            for x, y in self.seen:
                cv2.circle(out, (int(x), int(y)), 4, (0, 230, 0), -1)
        return out
