"""The sorting loop and everything the web page and the printer menu can ask for.

One cycle: photo position -> photo -> parts and markers -> did the last pick work? -> plan ->
pick and drop (or shake) -> again. Commands (start, pause, resume, stop, step) arrive from the web
page or from the printer's menu and are handled between moves, never in the middle of one: a
part in the picker is always dropped first.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import binspec, pickers
from . import files as files_mod
from .analysis import ModelChoice
from .binswap import BinRequest, after_ok, choose_swap
from .config import Machine
from .fill import BinFill, CatalogueVolumes, Packing, bin_room_mm3, part_volume_mm3
from .grip import jaw_angle
from .headcam import merge_markers, merge_parts, shift_part, tiles
from .io import read_json, write_json
from .layout import Layout, yaw_from_up
from .models import ModelError, ModelStore
from .parts import compute_gaps, to_bed
from .photos import Photos
from .printer import Motion, Printer, PrinterError
from .rules import family, wild_key
from .setup import KEYS as SETUP_KEYS
from .setup import Setup
from .strategies import STRATEGIES, Plan, Planner
from .todo import TodoList
from .transform import MIN_SPREAD_MM, BedCalibration, Transform2D
from .trayview import WorkArea, paint_outside, parse_colour
from .trayview import rect as tray_rect
from .vision import Shot, Vision

log = logging.getLogger("sortering_pnp.runner")


@dataclass
class State:
    status: str = "idle"  # idle | running | paused | stopping | done | stuck | error
    message: str = "ready"
    strategy: str = "sort"
    plan: str = ""
    picks: int = 0
    failed_picks: int = 0
    counts: dict[int, int] = field(default_factory=dict)
    shot: str | None = None  # file name of the latest picture with outlines
    timing: dict[str, float] = field(default_factory=dict)  # the last pick cycle (pipeline)
    parts: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)
    phase: str = ""  # what the machine does right now, in a few words
    target: dict[str, Any] | None = None  # the part it picks next (Device tab: boxed)
    view: str | None = None  # the latest photo without drawings (the page draws on it)
    view_h: list[list[float]] | None = None  # plate mm -> view pixels (3x3 homography)


class Runner:
    def __init__(
        self, m: Machine, printer: Printer, vision: Vision, store: ModelStore | None = None
    ) -> None:
        self.m = m
        self.printer = printer
        self.motion = Motion(printer, m)
        self.pins = self.motion.pins  # the picker, switched by sortering-pnp itself (pins.py)
        self.vision = vision
        self.store = store
        self.state = State(strategy=m.run.strategy)
        self.events: deque[str] = deque(maxlen=300)
        self.console: deque[str] = deque(maxlen=200)  # short lines: what it decides
        self._last_seen = ""
        self._lock = threading.RLock()  # the printer: one user at a time (loop or web page)
        self._thread: threading.Thread | None = None
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._now = threading.Event()  # automatic mode: Resume / Start, look and sort now
        self._in_auto = False
        self._shot_no = 0
        self.calib_pairs: list[dict[str, Any]] = []
        cal = self.calibration()
        if cal is not None:
            self.calib_pairs = list(cal.pairs)
        self.last_shot: Shot | None = None
        self._claims_lock = threading.RLock()
        # wild bin marker -> what it collects; bin marker -> parts in it since it was emptied
        self.claims, self.bin_counts, self.bin_volume = self._load_claims()
        self.volumes = self._load_volumes()
        self.ready: dict[int, dict[str, Any]] = {}  # bins that can be taken out
        self.requests: list[BinRequest] = self._loaded_requests  # bin changes, until the OK
        # a bin change takes two OKs (web page or printer menu): the first parks the machine
        # (bed out, hands safe), the second says the bins are changed: a photo checks them
        self._layout_changed = threading.Event()  # the page saved the layout while sorting
        self._park = threading.Event()  # first OK, while sorting: park after this move
        self._ok = threading.Event()  # second OK: the bins are changed
        self.swap_phase = ""  # "" sorting | parked (waiting for the second OK)
        self._no_swap_said = ""
        self.mask_checked = False  # the tray view was checked and saved (Calibrate tab)
        self._crop: dict[str, Any] = {}  # Camera cropping being tried (margin, colour), not saved
        self._layout_cache: tuple[tuple[str, int, int], Layout] | None = None
        self.markers_seen: set[int] = set()  # every code the camera has read
        self._load_tool()
        self._load_machine()
        setup_path = self.m.path(self.m.paths.setup)
        assert setup_path is not None
        self.setup = Setup(setup_path)
        todo_path = self.m.path(self.m.paths.todo)
        assert todo_path is not None
        self.todo = TodoList(todo_path)
        self.photos = Photos(self)  # calibration and real photos (Calibrate and Photos tabs)
        # the simulator needs no setup; a real printer does
        self.require_setup = self.m.printer.driver != "sim"
        self._setup_homed = False  # homed from the Setup tab since the program started
        self._setup_picker: set[str] = set()  # grab / release tried
        self._tool: str | None = None  # the tool Klipper said is mounted, when last asked
        self._calib_markers: list[dict[str, Any]] = []  # the codes in the last calibration photo
        self._calib_fit: dict[str, Any] | None = None  # the last Fit (Camera to printer)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyse")

    # ------------------------------------------------------------------ files
    @property
    def layout_path(self) -> Path:
        p = self.m.path(self.m.paths.layout)
        assert p is not None
        return p

    @property
    def transform_path(self) -> Path:
        p = self.m.path(self.m.paths.transform)
        assert p is not None
        return p

    @property
    def runs_dir(self) -> Path:
        p = self.m.path(self.m.paths.runs)
        assert p is not None
        return p

    def layout(self) -> Layout:
        """The layout file, parsed again only when it changed (the page asks for the state
        every second; parsing it each time costs the Pi's CPU and slows every answer). A copy:
        callers may change it."""
        p = self.layout_path
        try:
            st = p.stat()
            key = (str(p), st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return Layout.load(p)
        cached = self._layout_cache
        if cached is None or cached[0] != key:
            cached = self._layout_cache = (key, Layout.load(p))
        return copy.deepcopy(cached[1])

    def calibration(self) -> BedCalibration | None:
        return BedCalibration.load(self.transform_path)

    def transform(self) -> Transform2D:
        cal = self.calibration()
        if cal is not None:
            return cal.transform
        if self.m.printer.driver != "sim":
            if self.transform_path.is_file():  # saved, but unusable (e.g. one tip position)
                self.setup.reset("camera")
                raise PrinterError(
                    "Camera to printer is not usable (the tip was at the same place for every "
                    "code): do Setup step 6 again"
                )
            raise PrinterError("the camera is not calibrated to the printer yet: Setup step 6")
        return Transform2D.identity()

    # ------------------------------------------------------------------ status
    def note(self, text: str) -> None:
        log.info(text)
        self.events.append(time.strftime("%H:%M:%S ") + text)

    def say(self, text: str) -> None:
        """One short line on the page's console: what the machine sees and decides."""
        self.console.append(time.strftime("%H:%M:%S ") + text)

    def phase(self, text: str) -> None:
        """What the machine does right now (the Device tab's activity), and a console line."""
        self._set(phase=text)
        self.say(text)

    def _say_seen(self, parts: list[Any], layout: Layout) -> None:
        """What lies on the plate, grouped: '7 on the plate: 3 DIN912_M4x16 · 2 DIN934_M4'."""
        from collections import Counter

        on = [p for p in parts if layout.in_work(p.bed_xy, self.m.run.work_margin_mm)]
        kinds = Counter(
            p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "") for p in on
        )
        line = (
            f"{len(on)} on the plate: "
            + " · ".join(f"{n} {k}" for k, n in kinds.most_common(6))
            + (" …" if len(kinds) > 6 else "")
            if on
            else "plate empty"
        )
        if line != self._last_seen:
            self.say(line)
            self._last_seen = line

    def _set(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self.state, k, v)

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        return {
            "status": s.status,
            "message": s.message,
            "strategy": s.strategy,
            "plan": s.plan,
            "picks": s.picks,
            "failed_picks": s.failed_picks,
            "counts": {str(k): v for k, v in s.counts.items()},
            "shot": s.shot,
            "parts": s.parts,
            "markers": s.markers,
            "events": list(self.events)[-60:],
            "console": list(self.console)[-80:],
            "strategies": STRATEGIES,
            "calibrated": self.transform_path.is_file(),
            "calib_pairs": self.calib_pairs,
            "driver": self.m.printer.driver,
            **self.bins_state(),
            "requests": [r.to_dict() for r in self.requests],
            "mask_margin_mm": self.m.camera.mask_margin_mm,
            "swap_phase": self.swap_phase,
            "auto": self.saved_mode()[0],
            "tip_offset_z": self.m.tool.offset[2],
            "timing": self.state.timing,
            "phase": self.state.phase,
            "target": self.state.target,
            "view": self.state.view,
            "view_h": self.state.view_h,
            "setup": {
                "required": self.require_setup,
                "complete": not self.setup.missing(),
                "missing": self.setup.missing(),
            },
        }

    # ------------------------------------------------------------------ commands
    def command(self, cmd: str, strategy: str | None = None) -> str:
        cmd = (cmd or "").strip().lower()
        running = self._thread is not None and self._thread.is_alive()
        resuming = running and self.state.status in ("paused", "stuck") and cmd == "start"
        if (
            cmd in ("start", "step", "auto")
            and self.require_setup
            and self.setup.missing()
            and not resuming
        ):
            return "finish the Setup tab first: " + ", ".join(self.setup.missing())
        if cmd in ("start", "step"):
            if running:
                standby = self._in_auto and self.state.status in ("standby", "stuck", "error")
                if cmd == "start" and (self.state.status in ("paused", "stuck") or standby):
                    return self.command("resume")
                return "already running"
            strat = (strategy or self.state.strategy or self.m.run.strategy).lower()
            if strat not in STRATEGIES:
                return f"unknown strategy {strat!r}"
            self._pause.clear()
            self._stop.clear()
            self._set(
                strategy=strat,
                status="running",
                message=f"starting: {strat}",
                picks=0,
                failed_picks=0,
                counts={},
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(strat, 1 if cmd == "step" else None),
                name="sorter",
                daemon=True,
            )
            self._thread.start()
            return f"{cmd}: {strat}"
        if cmd == "auto":
            if running:
                return "already running: stop first"
            strat = (strategy or self.state.strategy or self.m.run.strategy).lower()
            if strat not in STRATEGIES:
                return f"unknown strategy {strat!r}"
            self._pause.clear()
            self._stop.clear()
            self._save_mode(True, strat)
            self._set(
                strategy=strat,
                status="standby",
                message="automatic mode",
                picks=0,
                failed_picks=0,
                counts={},
            )
            self._thread = threading.Thread(
                target=self._auto, args=(strat,), name="sorter", daemon=True
            )
            self._thread.start()
            return f"automatic: {strat}"
        if cmd in ("change", "change_bin"):
            cmd = "ok"
        if cmd == "ok":  # Change bin: the first press parks; the second checks the bins
            if self.swap_phase != "parked":
                self.swap_phase = "parked"
                if running:
                    self._park.set()
                    return "parking after this move; change the bins, then Change bin again"
                self.note("parked for a bin change")
                return "change the bins, then press Change bin again"
            if running:
                self._ok.set()
                return "checking the bins, then sorting on"
            self.swap_phase = ""
            res = self.bins_ok()
            return "; ".join(res["resolved"]) or "the bins were checked: nothing was asked for"
        if cmd == "pause":
            if running:
                self._pause.set()
                self._set(status="paused", message="pausing after this move")
            return "paused"
        if cmd == "resume":
            if running and self._in_auto and not self._pause.is_set():
                self._now.set()  # automatic mode waiting: look and sort now
                self._set(message="looking now: sorting what lies on the plate")
                return "looking now"
            if running:
                self._pause.clear()
                self._set(status="running", message="resumed")
            return "resumed"
        if cmd == "stop":
            self._save_mode(False)
            if running:
                self._stop.set()
                self._pause.clear()
                self._set(status="stopping", message="stopping after this move")
            return "stopping"
        if cmd in ("estop", "emergency_stop"):
            self._stop.set()
            self._pause.clear()
            self._save_mode(False)
            self.pins.off()
            try:
                self.printer.emergency_stop()
            finally:
                self._set(
                    status="error", message="EMERGENCY STOP: restart Klipper (FIRMWARE_RESTART)"
                )
                self.note("emergency stop")
            return "emergency stop sent"
        return f"unknown command {cmd!r}"

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ one photo
    def _photo_name(self) -> str:
        self._shot_no += 1
        return time.strftime("%Y%m%d-%H%M%S") + f"_{self._shot_no:04d}"

    def take_shot(
        self,
        layout: Layout,
        T: Transform2D | None,
        move: bool = True,
        area: str = "work",
        paint: bool = True,
    ) -> Shot:
        """Photo position, photo, parts (with printer X/Y when T is known), markers. `move`
        False: photograph where the bed is (standby with the motors off: still parked).
        A head camera photographs `area`: work (the tray), bins (the grid and around it) or
        here (one photo at the photo position: calibration). `paint`: the model sees only the
        tray's inside (trayview.py); False to look into the bins too."""
        work = self._work_area(layout, T) if paint else None
        if self.m.camera.source == "head":
            return self._head_shot(layout, T, area, work)
        with self._lock:
            if move:
                self.phase("parking at the photo position")
                self.motion.photo_position()
            name = self._photo_name()
            heights = lambda mid: _marker_plate_height(layout, mid)  # noqa: E731
            if hasattr(self.vision, "snap") and hasattr(self.vision, "analyse"):
                self.phase("taking a photo")
                photo = self.vision.snap(name)
                self.phase("running the model")
                shot = (
                    self.vision.analyse(photo, name, heights, work)
                    if work is not None
                    else self.vision.analyse(photo, name, heights)
                )
            else:
                self.phase("taking a photo")
                shot = (
                    self.vision.capture(name, heights, work)
                    if work is not None
                    else self.vision.capture(name, heights)
                )
        return self._finish_shot(shot, layout, T)

    def _work_area(self, layout: Layout, T: Transform2D | None) -> WorkArea | None:
        """The tray's inside (and its corner post) in the plate frame, for painting the rest."""
        if T is None or not self.m.camera.mask:
            return None
        Ti = T.inverse()
        g = self.m.camera.mask_margin_mm  # + keeps more of the wall, - paints more of the floor
        x0, y0, x1, y1 = layout.work_rect()
        post = layout.work_keepout()
        holes = []
        if post is not None:
            px0, py0, px1, py1 = post
            holes = [Ti.apply(tray_rect(px0 + g, py0 + g, px1 - g, py1 - g))]
        return WorkArea(Ti.apply(tray_rect(x0 - g, y0 - g, x1 + g, y1 + g)), holes)

    # ------------------------------------------------------------------ what the model sees
    def mask_preview(self, margin_mm: float) -> dict[str, Any]:
        """A photo painted with this margin (not saved): the Device tab's photo shows what the
        model sees."""
        self._idle()
        layout, T = self.layout(), self.transform()
        with self._lock:
            self.motion.preflight(home=False)
        old = self.m.camera.mask_margin_mm
        self.m.camera.mask_margin_mm = float(margin_mm)
        try:
            shot = self.take_shot(layout, T)
        finally:
            self.m.camera.mask_margin_mm = old
        return {
            "image": shot.vis.name if shot.vis else None,
            "parts": len(shot.parts),
            "margin_mm": float(margin_mm),
        }

    def remember_machine(self, values: dict[str, Any]) -> None:
        """Keep values set in the page in machine.json (over machine.yaml)."""
        p = self.m.path(self.m.paths.machine)
        assert p is not None
        kept = read_json(p) if p.is_file() else {}
        write_json(p, {**kept, **values})

    def camera_height(self, height_mm: float) -> dict[str, Any]:
        """Skip the camera calibration for now (Calibrate tab): the camera is taken as straight
        down over the picture's centre, this high above the tray floor. A calibration made
        later replaces it."""
        self._idle()
        h = float(height_mm)
        if not 100.0 <= h <= 1000.0:
            raise ValueError("the camera's height: lens to the tray floor, 100 to 1000 mm")
        self.m.camera.height_mm = h
        self.remember_machine({"camera_height_mm": h})
        reload = getattr(self.vision, "reload_setup", None)
        if reload is not None:
            reload()
        self.note(f"camera calibration skipped: straight down, {h:g} mm above the tray floor")
        return self.photos.state()

    def camera_flip(self, hflip: bool, vflip: bool) -> dict[str, Any]:
        """The picture flipped by the camera (both: turned 180 degrees), for photos and the live
        picture. What was fitted to the old picture no longer holds: Camera to printer (and a
        camera calibration) must be done again."""
        self._idle()
        c = self.m.camera
        if (c.hflip, c.vflip) == (bool(hflip), bool(vflip)):
            return self.photos.state()
        c.hflip, c.vflip = bool(hflip), bool(vflip)
        self.remember_machine({"camera_hflip": c.hflip, "camera_vflip": c.vflip})
        reopen = getattr(self.vision, "reopen_camera", None)
        if reopen is not None:
            reopen()
        self.calib_clear()
        self.setup.reset("camera")
        self.last_shot = None
        turn = {(True, True): "turned 180°", (True, False): "mirrored left-right"}
        turn |= {(False, True): "mirrored top-bottom", (False, False): "as the camera gives it"}
        self.note(f"camera picture {turn[(c.hflip, c.vflip)]}: do Camera to printer again")
        return self.photos.state()

    def calibration_settings(
        self, measured_square_mm: float | None, board_thickness_mm: float
    ) -> dict[str, Any]:
        c = self.m.calibration
        c.measured_square_mm = float(measured_square_mm) if measured_square_mm else None
        c.board_thickness_mm = float(board_thickness_mm)
        self.remember_machine(
            {
                "calibration": {
                    "measured_square_mm": c.measured_square_mm,
                    "board_thickness_mm": c.board_thickness_mm,
                }
            }
        )
        return self.photos.state()

    def calibration_replaced(self) -> None:
        """A new camera calibration: its plate frame starts at the board, so the old camera-to-
        printer fit is wrong now. It is kept aside, and the Setup step asks for it again."""
        if self.transform_path.is_file():
            aside = self.transform_path.with_name(
                self.transform_path.stem + time.strftime("_before_%Y%m%d-%H%M%S.json")
            )
            self.transform_path.replace(aside)
        self.calib_pairs = []
        self.setup.reset("camera")
        self.note("new camera calibration: redo Camera to printer (Calibrate tab)")

    def mask_save(self, margin_mm: float) -> dict[str, Any]:
        self.remember_machine({"mask_margin_mm": float(margin_mm)})
        self.m.camera.mask_margin_mm = float(margin_mm)
        self.mask_checked = True
        self.note(f"tray view saved: margin {float(margin_mm):g} mm")
        return {"margin_mm": float(margin_mm), "saved": True}

    # ------------------------------------------------------------------ camera cropping
    def crop_state(self) -> dict[str, Any]:
        """Camera cropping (Calibrate tab): only for the model that finds the parts. It needs the
        camera-to-printer fit and the bins (where the tray is); the camera calibration, or the
        straight-down camera at its height until then."""
        missing = []
        if self.calibration() is None and self.m.printer.driver != "sim":
            missing.append("Camera to printer (Setup tab, step 6)")
        if not self.setup.is_done("layout") and self.m.printer.driver != "sim":
            missing.append("the bins found (Setup tab, step 7)")
        c = self.m.camera
        return {
            "ready": not missing,
            "missing": missing,
            "margin_mm": float(self._crop.get("margin", c.mask_margin_mm)),
            "colour": str(self._crop.get("colour", c.mask_colour)),
            "saved_margin_mm": c.mask_margin_mm,
            "saved_colour": c.mask_colour,
            "changed": bool(self._crop),
        }

    def crop_preview(self, margin_mm: float, colour: str) -> dict[str, Any]:
        """Try a margin and a colour on the live picture (not saved)."""
        parse_colour(colour)  # auto or #RRGGBB, else ValueError
        self._crop = {"margin": float(margin_mm), "colour": colour}
        return self.crop_state()

    def crop_save(self, margin_mm: float, colour: str) -> dict[str, Any]:
        parse_colour(colour)
        self.mask_save(margin_mm)
        self.remember_machine({"mask_colour": colour})
        self.m.camera.mask_colour = colour
        self._crop = {}
        return self.crop_state()

    def crop_frame(self, frame: np.ndarray) -> np.ndarray:
        """A live frame painted and cut as the model will see it: the tray's inside only (the
        working area of the layout, through the camera-to-printer fit), with the margin and
        colour being tried."""
        from .photos import placeholder

        st = self.crop_state()
        cam = getattr(self.vision, "cam_full", None)
        calib = getattr(self.vision, "calibration", None)
        if not st["ready"] or cam is None:
            return placeholder("Camera cropping needs: " + ", ".join(st["missing"] or ["a camera"]))
        c = self.m.camera
        old = (c.mask, c.mask_margin_mm)
        c.mask, c.mask_margin_mm = True, st["margin_mm"]
        try:
            work = self._work_area(self.layout(), self.transform())
        finally:
            c.mask, c.mask_margin_mm = old
        if work is None:
            return placeholder("no working area: Setup tab, step 7 (Find the bins)")
        s = frame.shape[1] / float(cam.image_size[0])

        def px(poly: np.ndarray) -> np.ndarray:
            return cam.project(np.c_[poly, np.zeros(len(poly))]) * s

        img = paint_outside(
            frame, px(work.outline), [px(h) for h in work.holes], parse_colour(st["colour"])
        )
        roi = (calib or {}).get("roi")
        if roi:  # the model gets this part of the photo (its size unchanged)
            x0, y0 = int(roi["x0"] * s), int(roi["y0"] * s)
            img = img[y0 : y0 + int(roi["h"] * s), x0 : x0 + int(roi["w"] * s)]
        return img

    # ------------------------------------------------------------------ on the machine
    def todo_state(self) -> dict[str, Any]:
        return {"items": self.todo.state(self)}

    def todo_tick(self, key: str, done: bool) -> dict[str, Any]:
        self.todo.tick(key, done)
        return self.todo_state()

    def _head_shot(
        self, layout: Layout, T: Transform2D | None, area: str, work: WorkArea | None = None
    ) -> Shot:
        """The head camera over `area`: overlapping photos, joined in the frame of the
        calibration photo (headcam.py)."""
        hc = self.m.camera.head
        pc = self.m.printer
        ref = np.asarray(pc.photo_position[:2], dtype=float)
        if area == "here" or T is None:
            spots = [tuple(ref)]
        else:
            if area == "bins":
                k = hc.margin_cells
                rect = layout.cells_rect(-k, -k, layout.cols + 2 * k, layout.rows + 2 * k)
            else:
                rect = layout.work_rect()
            ox, oy = hc.offset
            spots = [
                (
                    min(max(x - ox, pc.limits_x[0]), pc.limits_x[1]),
                    min(max(y - oy, pc.limits_y[0]), pc.limits_y[1]),
                )
                for x, y in tiles(rect, hc.fov_mm, hc.overlap)
            ]
        Ai = np.linalg.inv(T.A) if T is not None else np.eye(2)
        name = self._photo_name()
        parts: list[Any] = []
        markers: list[Any] = []
        last: Shot | None = None
        for k, (x, y) in enumerate(spots):
            with self._lock:
                self.motion.travel(x, y)
                self.motion.move(z=pc.photo_position[2])
                self.printer.gcode("M400")
            d = Ai @ (np.array([x, y]) - ref)  # the view moved with the head
            with self._lock:
                last = self.vision.capture(
                    f"{name}_{k:02d}",
                    lambda mid: _marker_plate_height(layout, mid),
                    work.shifted(-d) if work is not None else None,
                )
            for p in last.parts:
                shift_part(p, d)
            for mk in last.markers:
                mk.centre = (mk.centre[0] + d[0], mk.centre[1] + d[1])
            parts += last.parts
            markers += last.markers
        assert last is not None
        shot = Shot(
            name,
            merge_parts(parts, hc.same_mm),
            merge_markers(markers),
            last.photo,
            last.vis,
        )
        return self._finish_shot(shot, layout, T)

    def _finish_shot(self, shot: Shot, layout: Layout, T: Transform2D | None) -> Shot:
        self.markers_seen |= {mk.marker for mk in shot.markers}
        if T is not None:
            to_bed(shot.parts, T)
        compute_gaps(shot.parts)
        self.last_shot = shot
        self._prune_photos()
        self._set(
            shot=shot.vis.name if shot.vis else None,
            view=shot.view.name if getattr(shot, "view", None) else None,
            view_h=getattr(shot, "view_h", None),
            parts=[
                {
                    "id": p.id,
                    "cls": p.cls,
                    "poly": _poly_for_page(p.polygon),
                    "xy": [round(float(v), 1) for v in p.plate_xy],
                    "length": p.length_mm,
                    "score": round(p.score, 2),
                    "bed": [round(v, 1) for v in p.bed_xy] if T is not None else None,
                    "gap": None if math.isinf(p.gap_mm) else round(p.gap_mm, 1),
                    "in_work": bool(
                        T is not None and layout.in_work(p.bed_xy, self.m.run.work_margin_mm)
                    ),
                }
                for p in shot.parts
            ],
            markers=[
                {"marker": mk.marker, "plate": [round(v, 2) for v in mk.centre]}
                for mk in shot.markers
            ],
        )
        return shot

    def _prune_photos(self) -> None:
        keep = self.m.run.keep_photos
        if keep <= 0 or not self.runs_dir.is_dir():
            return
        photos = sorted(self.runs_dir.glob("*_[0-9][0-9][0-9][0-9].jpg"))
        for p in photos[:-keep]:
            for f in (p, p.with_name(p.stem + "_vis.jpg")):
                f.unlink(missing_ok=True)

    # ------------------------------------------------------------------ the loop
    def _run(self, strategy: str, steps: int | None) -> None:
        try:
            outcome = self._sort(strategy, steps)
            if outcome != "done":
                self._set(status="idle", message="stopped" if self._stop.is_set() else "finished")
        except Exception as exc:  # noqa: BLE001 - shown on the web page, the loop must not die silently
            self.pins.off()
            log.exception("sorting stopped")
            self.note(f"error: {exc}")
            self._set(status="error", message=str(exc))

    def _prepare(self) -> tuple[Layout, Transform2D]:
        layout = self.layout()
        T = self.transform()
        x0, y0, x1, y1 = layout.work_rect()
        self.motion.z_home_xy = ((x0 + x1) / 2, (y0 + y1) / 2)  # Z is homed over the plate
        return layout, T

    def _sort(self, strategy: str, steps: int | None, auto: bool = False) -> str:
        """Sort until the working area is empty: "done", "stuck" (only when `auto`: then the
        caller waits for the parts to change), "stopped" or "steps"."""
        missing = getattr(self.vision, "model_missing", lambda: None)()
        if missing:
            raise PrinterError(missing)
        layout, T = self._prepare()
        with self._lock:
            self.motion.preflight()
            if self.m.printer.accel:
                self.printer.gcode(f"SET_VELOCITY_LIMIT ACCEL={self.m.printer.accel:.0f}")
            pk = self.m.picker
            if pk.kind.rotates and pk.rotate_macro and pk.rotate_home:  # a stepper finds its 0
                self.printer.gcode(pk.rotate_home)
        ok, why = self.pins.status()
        if not ok:
            raise PrinterError(f"the picker's pins: {why}")
        first: Shot | None = None
        run = self.m.run
        if run.scan_bins_on_start or run.only_seen_bins:
            first = self.take_shot(layout, T, area="bins")
            seen = {mk.marker for mk in first.markers}
            if run.scan_bins_on_start:
                moved = self._place_bins(layout, first, T)
                added = [b["marker"] for b in moved if b.get("new")]
                layout.save(self.layout_path)
                if added:
                    self.note(f"new bins seen, used as wild bins: {', '.join(map(str, added))}")
            if run.only_seen_bins:
                # the plate stays even when a part lies on its code: it is never a target
                keep = seen | ({layout.work_marker} if layout.work_marker is not None else set())
                missing = sorted(b.marker for b in layout.bins if b.marker not in keep)
                if missing:
                    self.note(f"bins not seen by the camera, not used: {missing}")
                layout.bins = [b for b in layout.bins if b.marker in keep]
                if not [b for b in layout.bins if b.role != "work"]:
                    raise PrinterError("the camera sees no bins (markers covered or dirty?)")
        problems = layout.problems()
        if problems:
            raise PrinterError("layout: " + "; ".join(problems))
        planner = Planner(layout, self.m, strategy)
        self._park.clear()  # an OK left from before this run
        self._ok.clear()
        self.swap_phase = ""
        wild = {b.marker for b in layout.bins if b.role == "wild"}
        with self._claims_lock:
            for mk in [k for k in self.claims if k not in wild and layout.bin_by_marker(k)]:
                del self.claims[mk]  # no longer a wild bin
            planner.claims = self.claims  # shared: emptied bins apply at once
        self._set(status="running")
        self.note(f"started: {STRATEGIES[strategy]}")
        self.say(f"sorting: {strategy}")
        self._last_seen = ""
        prev: Plan | None = None
        done_steps = 0
        waiting_for = ""  # the bin change last announced
        while not self._stop.is_set():
            if self._pause.is_set():
                self._set(
                    status="paused", message="paused: resume from the web page or the printer menu"
                )
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                if self._stop.is_set():
                    break
                self._set(status="running")
                planner.shakes_in_row = 0
                prev = None  # someone may have moved parts
                first = None  # ... so a photo from before the pause is out of date
            if self._park.is_set():  # first OK: park and wait for the second
                self._park.clear()
                if self._parked_for_bins():
                    break
                prev = None
                first = None
            if self._layout_changed.is_set():  # changes from the page: use them from now on
                self._layout_changed.clear()
                layout = self.layout()
                planner.layout = layout
                self.note("layout changes applied")
            if self._ok.is_set():  # after a bin change: a new photo, with the bins
                first = None
            check = self._ok.is_set()  # it must look into the bins: nothing painted over
            area = "bins" if check else "work"
            shot, first = (
                (first, None)
                if first is not None
                else (self.take_shot(layout, T, area=area, paint=not check), None)
            )
            if self._ok.is_set():  # a bin was changed: this photo shows what stands there now
                self._ok.clear()
                self._resolve_requests(layout, shot, T)
                planner.claims = self.claims
                prev = None
            with self._claims_lock:
                before = dict(self.claims)
                ok = prev is None or planner.report(prev, shot.parts)
                if self.claims != before:
                    self._save_claims()
                    mk = next(k for k in self.claims if before.get(k) != self.claims[k])
                    self.note(f"wild bin {mk} now collects {self.claims[mk]}")
                    self.say(f"bin {mk} now holds {self.claims[mk]}")
            if not ok and prev is not None and prev.part is not None:
                self.say(f"missed {prev.part.cls}: try again")
            if not ok:
                self.note(
                    f"pick of {prev.part.cls if prev and prev.part else '?'} failed: the part is still there"
                )
            if ok and prev is not None and prev.kind == "pick" and prev.bin is not None:
                with self._claims_lock:
                    mk = prev.bin.marker
                    self.bin_counts[mk] = self.bin_counts.get(mk, 0) + 1
                    if prev.part is not None:
                        vol = part_volume_mm3(prev.part, self.m.run.fill, self.volumes)
                        self.bin_volume[mk] = self.bin_volume.get(mk, 0.0) + vol
                        self.bin_family[mk] = family(prev.part.cls)
                    self._save_claims()
            self._set(
                counts=dict(planner.sorted_count),
                failed_picks=planner.failed_picks,
                picks=sum(planner.sorted_count.values()),
            )
            self._update_ready(planner, layout, shot.parts)
            planner.full = {mk for mk, f in self.bin_fill(layout).items() if f["full"]}
            self._say_seen(shot.parts, layout)
            if steps is not None and done_steps >= steps:
                return "steps"
            here = tuple(self.motion.current_tip()[:2])
            plan = planner.next(shot.parts, here)  # type: ignore[arg-type]
            self._set(plan=_describe(plan), target=self._target(plan, layout))
            if plan.note:
                self.note(plan.note)
                self.say(plan.note)
            self._ask_bins(planner, layout, shot.parts)
            if plan.kind == "wait_bin":  # everything else is sorted: only a bin change helps
                asked = "; ".join(r.text() for r in self.requests) or plan.reason
                if asked != waiting_for:  # say it once, not on every look
                    waiting_for = asked
                    self.note(f"waiting for a bin change: {plan.reason}")
                    self.say("waiting: " + asked)
                    self.motion.message("Change bin: press it")
                self._set(status="waiting", message=asked + "; press Change bin to park")
                # look again now and then: parts thrown in meanwhile are sorted while it waits
                end = time.monotonic() + self.m.standby.interval_s
                while (
                    not (self._park.is_set() or self._ok.is_set())
                    and not self._stop.is_set()
                    and time.monotonic() < end
                ):
                    time.sleep(0.2)
                self._set(status="running")
                prev = None
                continue
            waiting_for = ""
            if plan.kind == "done":
                self._set(status="done", message=plan.reason)
                self.note(f"done: {plan.reason}")
                self.say("done: " + plan.reason)
                self.motion.message("sortering-pnp: done")
                return "done"
            if plan.kind == "stuck":
                self.note(plan.reason)
                self.say("stuck: " + plan.reason)
                self.motion.message("sortering-pnp: needs a hand")
                if auto:
                    self._set(
                        status="stuck", message=plan.reason + "; waiting until they are moved"
                    )
                    return "stuck"
                self._set(status="stuck", message=plan.reason + "; then press resume")
                self._pause.set()
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                planner.shakes_in_row = 0
                prev = None
                first = None
                self._set(status="running")
                continue
            if plan.kind == "shake":
                self.say("shake the bed: " + plan.reason.split(":")[0])
                with self._lock:
                    self.note(plan.reason)
                    self.motion.shake()
            elif (
                self.m.run.pipeline
                and hasattr(self.vision, "snap")
                and self.m.camera.source != "head"  # it travels with the part in the picker
            ):
                first = self._pick_photo_drop(plan, layout, T)
            else:
                with self._lock:
                    self._pick(plan, layout)
                    self._drop(plan, layout)
            prev = plan
            done_steps += 1
        return "stopped"

    # ------------------------------------------------------------------ automatic mode
    def _wait(self, seconds: float, wake: bool = False) -> bool:
        """Sleep, waking up for stop (and with `wake`, for Resume/Start: look now); True when
        stopped."""
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set():
            left = end - time.monotonic()
            if left <= 0 or (wake and self._now.is_set()):
                return False
            self._stop.wait(min(left, 0.2))
        return True

    def _look(self) -> tuple[Shot, list[Any], Layout]:
        """A standby photo. With the motors off nothing moves: the bed is still parked out."""
        layout, T = self._prepare()
        st = self.printer.status(None)
        homed = st.ready and all(a in st.homed for a in "xyz")
        if self.m.camera.source == "head" and not homed:  # it must move to look
            with self._lock:
                self.motion.preflight()
        shot = self.take_shot(layout, T, move=homed)
        margin = self.m.run.work_margin_mm
        return shot, [p for p in shot.parts if layout.in_work(p.bed_xy, margin)], layout

    def _auto(self, strategy: str) -> None:
        """Standby: look for parts every standby.interval_s; when some lie still on the plate, sort
        them; then wait again. Runs until Stop (or too many errors in a row)."""
        self._in_auto = True
        try:
            self._auto_loop(strategy)
        finally:
            self._in_auto = False

    def _auto_loop(self, strategy: str) -> None:
        sb = self.m.standby
        tried: list[Any] | None = None  # the parts of a stuck plate: wait until they change
        errors = 0
        self.note(f"automatic mode on: {STRATEGIES[strategy]}")
        self.phase("automatic: waiting for parts")
        self._now.clear()
        while not self._stop.is_set():
            try:
                if self._pause.is_set():
                    self._set(status="paused", message="automatic mode paused")
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    tried = None
                    continue
                now = self._now.is_set()  # Resume / Start: sort what is there, at once
                self._now.clear()
                if now:
                    tried = None
                _, parts, _ = self._look()
                if len(parts) >= sb.min_parts and not (
                    tried is not None and same_parts(parts, tried)
                ):
                    still = now
                    if not now:
                        self.say(f"{len(parts)} part(s) seen: waiting until they lie still")
                        self._set(
                            status="standby",
                            message=f"{len(parts)} part(s) seen: waiting until they lie still",
                        )
                        if self._wait(sb.settle_s, wake=True):
                            break
                        if self._now.is_set():
                            self._now.clear()
                            still = True
                        else:
                            _, again, _ = self._look()
                            still = same_parts(parts, again)
                            if not still:
                                self.say(
                                    f"not still yet: {len(parts)} then {len(again)} part(s), "
                                    "moved (Resume sorts them now)"
                                )
                    if still:
                        outcome = self._sort(strategy, None, auto=True)
                        tried = None
                        if outcome == "stuck" and self.last_shot is not None:
                            _, tried, _ = self._look()
                        if outcome == "stopped":
                            break
                        if sb.motors_off and self.m.camera.source != "head":
                            with self._lock:
                                self.motion.motors_off()
                        errors = 0
                        continue  # look again at once: more parts may have arrived meanwhile
                errors = 0
                nxt = time.strftime("%H:%M:%S", time.localtime(time.time() + sb.interval_s))
                status = "stuck" if tried is not None else "standby"
                msg = "parts need a hand" if tried is not None else "waiting for parts"
                self._set(status=status, message=f"{msg} · next look at {nxt} (Resume: now)")
                if self._wait(sb.interval_s, wake=True):
                    break
            except Exception as exc:  # noqa: BLE001 - shown on the page; standby keeps trying
                errors += 1
                log.exception("automatic mode")
                self.note(f"error ({errors}/{sb.max_errors}): {exc}")
                self._set(status="error", message=str(exc))
                if errors >= sb.max_errors:
                    self.note("automatic mode switched off after repeated errors")
                    self._save_mode(False)
                    return
                if self._wait(sb.interval_s):
                    break
        self._set(status="idle", message="automatic mode off")

    # ------------------------------------------------------------------ remembered settings
    @property
    def state_path(self) -> Path:
        p = self.m.path(self.m.paths.state)
        assert p is not None
        return p

    def _save_mode(self, auto: bool, strategy: str | None = None) -> None:
        write_json(self.state_path, {"auto": auto, "strategy": strategy or self.state.strategy})

    def saved_mode(self) -> tuple[bool, str]:
        """Was automatic mode on when the program stopped? (it is switched on again at start)"""
        if not self.state_path.is_file():
            return False, self.m.run.strategy
        d = read_json(self.state_path) or {}
        return bool(d.get("auto")), str(d.get("strategy") or self.m.run.strategy)

    def shutdown(self) -> None:
        """The program ends: stop after this move, but remember automatic mode for the restart."""
        self._stop.set()
        self._pause.clear()
        self.wait(30)
        self.pins.close()

    def _load_tool(self) -> None:
        p = self.m.path(self.m.paths.tool)
        if p is not None and p.is_file():
            d = read_json(p) or {}
            if "offset_z" in d:
                self.m.tool.offset[2] = float(d["offset_z"])

    def tip_touches_plate(self) -> dict[str, Any]:
        """The tip was jogged down until it just touches the TPU plate: from now on that is the
        plate's height, whatever Z the homing gives (see "Homing" in docs/reference.md)."""
        self._idle()
        layout = self.layout()
        with self._lock:
            pos = self.printer.status(None).position
            self.motion.tip = None
        offset_z = round(layout.surface_z() - pos[2], 3)
        p = self.m.path(self.m.paths.tool)
        assert p is not None
        write_json(
            p,
            {
                "offset_z": offset_z,
                "toolhead_z": pos[2],
                "measured": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        self.m.tool.offset[2] = offset_z
        self.setup.mark("tip", f"offset {offset_z:+.2f} mm")
        self.note(
            f"tip height: touches the plate at toolhead Z {pos[2]:.2f} (offset {offset_z:+.2f} mm)"
        )
        return {"offset_z": offset_z, "toolhead_z": pos[2]}

    def _pick_photo_drop(self, plan: Plan, layout: Layout, T: Transform2D) -> Shot:
        """Pick, photo with the part in the picker, then drop it while the photo is analysed.
        Returns the analysed photo: the next plan starts from it (and it shows whether the pick
        worked: the part is gone from the plate)."""
        t0 = time.perf_counter()
        with self._lock:
            self._pick(plan, layout)
            self.phase("taking a photo, the part in the picker")
            self.motion.photo_position()
            name = self._photo_name()
            photo = self.vision.snap(name)
        self.phase("running the model while the part is carried to its bin")
        t_photo = time.perf_counter()
        heights = lambda mid: _marker_plate_height(layout, mid)  # noqa: E731
        job = self._pool.submit(
            self.vision.analyse, photo, name, heights, self._work_area(layout, T)
        )
        with self._lock:
            self._drop(plan, layout)
        t_drop = time.perf_counter()
        shot = job.result()
        t_end = time.perf_counter()
        self._set(
            timing={
                "cycle_s": round(t_end - t0, 2),
                "move_s": round((t_photo - t0) + (t_drop - t_photo), 2),
                "waited_for_analysis_s": round(t_end - t_drop, 2),
            }
        )
        return self._finish_shot(shot, layout, T)

    def _pick(self, plan: Plan, layout: Layout) -> None:
        p, b = plan.part, plan.bin
        assert p is not None and b is not None
        pk = self.m.picker
        surface = layout.surface_z()
        if pk.kind.holds == "jaws":
            h = p.grip_height_mm if p.grip_height_mm is not None else p.top_mm * pk.grip_height
            z = surface + h
        else:
            z = surface + p.top_mm - pk.depth_mm
        z = max(z, surface + 0.3)
        x, y = p.grip_bed if p.grip_method != "centre" else p.bed_xy
        jaws = jaw_angle(p.bed_angle, pk.rotate_offset_deg)
        name = p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")
        self.note(f"pick {name} at ({x:.1f}, {y:.1f}) -> bin {b.marker} ({plan.reason})")
        turn = f", jaws {jaws:.0f}°" if pk.kind.rotates else ""
        self.phase(f"picking {name} at X{x:.0f} Y{y:.0f}{turn}, for bin {layout.bin_name(b)}")
        self.motion.message(f"pick {name}")
        self.motion.pick(x, y, z, jaws)

    def _drop(self, plan: Plan, layout: Layout) -> None:
        b = plan.bin
        assert b is not None
        cx, cy = layout.bin_centre(b)
        p = plan.part
        name = (p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")) if p else "it"
        self.phase(f"leaving {name} in bin {layout.bin_name(b)} ({b.marker})")
        self.motion.drop(cx, cy, layout.bin_rim_z(b))

    # ------------------------------------------------------------------ web-page helpers
    def _idle(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise PrinterError("the sorter is running: stop it first")

    def jog(self, dx: float, dy: float, dz: float) -> list[float]:
        self._idle()
        with self._lock:
            self.motion.preflight(home=False)
            return [round(v, 3) for v in self.motion.jog(dx, dy, dz)]

    def tip(self) -> list[float]:
        with self._lock:
            self.motion.tip = None
            return [round(v, 3) for v in self.motion.current_tip()]

    def calib_photo(self) -> list[dict[str, Any]]:
        self._idle()
        layout = self.layout()
        cal = self.calibration()
        with self._lock:
            self.motion.preflight(home=False)
        shot = self.take_shot(layout, cal.transform if cal else None, area="here", paint=False)
        out = []
        for mk in sorted(shot.markers, key=lambda m: m.marker):
            size = binspec.size_for_marker(mk.marker)
            d: dict[str, Any] = {
                "marker": mk.marker,
                "plate": [round(v, 2) for v in mk.centre],
                "what": size.name if size else "calibration board" if mk.marker < 50 else "?",
                "view_px": [round(v, 1) for v in mk.view_px] if mk.view_px else None,
            }
            if cal is not None:
                d["bed"] = [round(float(v), 2) for v in cal.transform.apply(mk.centre)]
            out.append(d)
        self._calib_markers = out
        self.say(f"camera: {len(out)} code(s) seen" if out else "camera: no codes seen")
        return out

    def calib_record(self, marker: int) -> dict[str, Any]:
        """Pair the camera's view of `marker` with where the tool tip is now (jogged onto it)."""
        if self.last_shot is None:
            raise PrinterError("take a calibration photo first")
        mk = next((x for x in self.last_shot.markers if x.marker == marker), None)
        if mk is None:
            raise PrinterError(f"marker {marker} was not in the last photo")
        tip = self.tip()
        for p in self.calib_pairs:
            far = math.dist(p["plate"], mk.centre) > MIN_SPREAD_MM
            if p["marker"] != marker and far and math.dist(p["bed"], tip[:2]) < 5.0:
                raise PrinterError(
                    f"the tip is where it was for code {p['marker']}: jog it onto the centre of "
                    f"code {marker} first"
                )
        self.calib_pairs = [p for p in self.calib_pairs if p["marker"] != marker]
        pair = {"marker": marker, "plate": [round(v, 3) for v in mk.centre], "bed": tip[:2]}
        self.calib_pairs.append(pair)
        self.note(
            f"calibration point: marker {marker} plate {pair['plate']} -> printer {pair['bed']}"
        )
        return pair

    def calib_fit(self) -> dict[str, Any]:
        if len(self.calib_pairs) < 3:
            raise PrinterError("record at least 3 markers (spread over the bed)")
        try:
            cal = BedCalibration.fit(self.calib_pairs)
        except ValueError as exc:
            self.setup.reset("camera")
            raise PrinterError(str(exc)) from exc
        cal.save(self.transform_path)
        worst = max(cal.residuals_mm)
        self.note(f"calibrated: {len(self.calib_pairs)} points, worst error {worst:.2f} mm")
        if worst < 1.0:
            self.setup.mark("camera", f"{len(self.calib_pairs)} points, worst {worst:.2f} mm")
        else:
            self.setup.reset("camera")
        self._calib_fit = {
            "residuals_mm": cal.residuals_mm,
            "worst_mm": round(worst, 2),
            "angle_deg": round(cal.transform.angle_deg, 3),
            "scale": round(cal.transform.scale, 5),
        }
        return self._calib_fit

    def calib_clear(self) -> None:
        self.calib_pairs = []
        self._calib_fit = None

    def goto_marker(self, marker: int) -> list[float]:
        """Check the calibration: put the tip 2 mm above a marker."""
        self._idle()
        if self.last_shot is None:
            raise PrinterError("take a calibration photo first")
        mk = next((x for x in self.last_shot.markers if x.marker == marker), None)
        if mk is None:
            raise PrinterError(f"marker {marker} was not in the last photo")
        layout = self.layout()
        T = self.transform()
        x, y = (float(v) for v in T.apply(mk.centre))
        b = layout.bin_by_marker(marker)
        z = layout.marker_z(b.height_u if b else 3) + 2.0
        with self._lock:
            self.motion.preflight(home=False)
            self.motion.travel(x, y)
            self.motion.move(z=z)
            self.printer.gcode("M400")
        return [round(x, 2), round(y, 2), round(z, 2)]

    def scan_bins(self) -> dict[str, Any]:
        """Photo, then put every bin whose marker is seen where it really is."""
        self._idle()
        layout = self.layout()
        T = self.transform()
        with self._lock:
            self.motion.preflight(home=False)
        shot = self.take_shot(layout, T, area="bins")
        report = layout.resync(self._seen_bins(layout, shot, T))
        layout.save(self.layout_path)
        wb = layout.work_bin()
        plate = f"bin {wb.marker} ({wb.w}x{wb.d})" if wb else "none"
        self.note(
            f"resync: {len(report['found'])} bin(s), grid at {report['origin']}, plate {plate}"
            + (f", not seen: {report['removed']}" if report["removed"] else "")
        )
        problems = layout.problems()
        if report["found"] and wb is not None and not problems:
            self.setup.mark("layout", f"{len(report['found'])} bins, plate {wb.marker}")
        else:
            self.setup.reset("layout")
        bins = [{"marker": b.marker, "x": b.x, "y": b.y, "rot": b.rot} for b in layout.bins]
        return {**report, "bins": bins, "problems": problems}

    # ------------------------------------------------------------------ the Setup tab
    MACHINE_KEYS = (
        "photo_position",
        "change_position",
        "travel_z",
        "limits_x",
        "limits_y",
        "limits_z",
    )

    def _load_machine(self) -> None:
        p = self.m.path(self.m.paths.machine)
        if p is None or not p.is_file():
            return
        for k, v in (read_json(p) or {}).items():
            if k in self.MACHINE_KEYS:
                setattr(self.m.printer, k, v)
            elif k == "mask_margin_mm":
                self.m.camera.mask_margin_mm = float(v)
                self.mask_checked = True
            elif k == "mask_colour":  # Camera cropping, Calibrate tab
                self.m.camera.mask_colour = str(v)
            elif k in ("camera_hflip", "camera_vflip"):  # Calibrate tab: Turn 180 / Mirror
                setattr(self.m.camera, k.removeprefix("camera_"), bool(v))
            elif k == "camera_height_mm":  # Calibrate tab, Skip for now
                self.m.camera.height_mm = float(v)
            elif k == "picker_mode":  # chosen in the Setup tab
                self.m.picker.set_type(str(v))
            elif k == "rotate_offset_deg":
                self.m.picker.rotate_offset_deg = float(v)
            elif k == "camera_settings":  # locked in the Calibrate tab
                self.m.camera.settings = str(v)
            elif k == "calibration":
                c = self.m.calibration
                c.measured_square_mm = v.get("measured_square_mm", c.measured_square_mm)
                c.board_thickness_mm = float(v.get("board_thickness_mm", c.board_thickness_mm))

    def machine_values(self) -> dict[str, Any]:
        return {k: getattr(self.m.printer, k) for k in self.MACHINE_KEYS}

    def save_machine(self, values: dict[str, Any]) -> dict[str, Any]:
        """Limits, travel height and photo position from the Setup tab (kept in data/
        machine.json, over machine.yaml). Un-checks the Machine step: confirm it again."""
        self._idle()
        new = self.machine_values()
        for k, v in values.items():
            if k not in self.MACHINE_KEYS:
                raise ValueError(f"unknown machine value {k!r}")
            if k == "travel_z":
                new[k] = float(v)
            else:
                vals = [float(x) for x in v]
                position = k.endswith("_position")
                if len(vals) != (3 if position else 2):
                    raise ValueError(f"{k}: wrong number of values")
                if not position and not vals[0] < vals[1]:
                    raise ValueError(f"{k}: the minimum must be below the maximum")
                new[k] = vals
        old = self.machine_values()
        for k, v in new.items():
            setattr(self.m.printer, k, v)
        try:
            for key in ("photo_position", "change_position"):
                x, y, z = new[key]
                self.motion.check(x, y, z)
                self.motion.check(x, y, new["travel_z"])
        except PrinterError:
            for k, v in old.items():
                setattr(self.m.printer, k, v)
            raise
        p = self.m.path(self.m.paths.machine)
        assert p is not None
        kept = read_json(p) if p.is_file() else {}  # e.g. the tray view's margin
        write_json(p, {**kept, **new})
        self.setup.reset("machine")
        self.note("machine values saved: confirm them in the Setup tab")
        return self.setup_state()

    def setup_state(self) -> dict[str, Any]:
        return {
            "steps": self.setup.state(),
            "required": self.require_setup,
            "complete": not self.setup.missing(),
            "machine": self.machine_values(),
            "homed_now": self._setup_homed,
            "picker_tried": sorted(self._setup_picker),
            "tool": self._tool,  # the mounted tool, as Klipper last said (None: not asked yet)
            "tip_offset_z": self.m.tool.offset[2],
            "camera": {
                "calibrated": self._camera_calibrated(),
                "markers": self._calib_markers,
                "pairs": self.calib_pairs,
                "fit": self._calib_fit,
                "view": self.state.view,
                "view_h": self.state.view_h,
            },
            "rotation": self.m.picker.kind.rotates,
            "picker": {
                "mode": self.m.picker.kind.key,
                "types": [pickers.as_dict(t) for t in pickers.PICKERS],
                "offset_deg": self.m.picker.rotate_offset_deg,
                "action_pin": self.m.picker.action_pin,
                "turn_pin": self.m.picker.turn_pin,
                "pins": dict(zip(("ok", "message"), self.pins.status(), strict=True)),
            },
        }

    def _camera_calibrated(self) -> str | None:
        """Where the camera calibration (the board in the tray) comes from, or None."""
        key = "head_calibration" if self.m.camera.source == "head" else "calibration"
        path, source = files_mod.find(self.m, key)
        return source if path is not None else None

    def setup_picker_type(self, mode: str) -> dict[str, Any]:
        """The kind of picker (Setup tab): how it takes a part, where it aims, whether it turns.
        Un-checks the Picker step: test it again. The pins follow the type by themselves."""
        self._idle()
        t = pickers.picker_type(mode)
        self.m.picker.set_type(t.key)
        self.remember_machine({"picker_mode": t.key})
        self.setup.reset("picker")
        self._setup_picker.clear()
        self.note(
            f"picker: {t.title} (aims at the {'head' if t.aim == 'head' else 'centre of gravity'})"
        )
        return self.setup_state()

    def setup_picker_offset(self, deg: float) -> dict[str, Any]:
        """Trim of the rotation: the servo angle at which the jaws close along printer X."""
        self._idle()
        self.m.picker.rotate_offset_deg = float(deg) % 180.0
        self.remember_machine({"rotate_offset_deg": self.m.picker.rotate_offset_deg})
        self._setup_picker.discard("rotate")
        return self.setup_state()

    def setup_check_printer(self) -> dict[str, Any]:
        """Only questions to Klipper, nothing moves."""
        pc = self.m.printer
        checks: list[dict[str, Any]] = []
        try:
            st = self.printer.status(pc.tool_macro)
        except PrinterError as exc:
            checks.append({"what": "Moonraker answers", "ok": False, "detail": str(exc)})
            st = None
        if st is not None:
            checks.append(
                {"what": "Klipper is ready", "ok": st.ready, "detail": st.message or st.state}
            )
            if st.ready:
                if pc.tool_macro:  # which tool is on: the Picker step (it has the button)
                    self._tool = st.tool
                    checks.append(
                        {
                            "what": "sortering_pnp.cfg is included",
                            "ok": st.tool is not None,
                            "detail": "[include sortering_pnp.cfg] in printer.cfg",
                        }
                    )
                hot = st.hotend_c
                checks.append(
                    {
                        "what": "the hotend is cold",
                        "ok": hot is None or hot <= pc.max_hotend_c,
                        "detail": f"{hot} C",
                    }
                )
                checks.append({"what": "nothing is printing", "ok": not st.printing, "detail": ""})
        ok = bool(checks) and all(c["ok"] for c in checks)
        if ok:
            self.setup.mark("printer")
        else:
            self.setup.reset("printer")
        return {"ok": ok, "checks": checks, **self.setup_state()}

    def _ask_tool(self) -> str | None:
        """The tool Klipper has as mounted ("picker", "hotend"); None without a tool macro."""
        pc = self.m.printer
        if not pc.tool_macro:
            return None
        self._tool = self.printer.status(pc.tool_macro).tool
        return self._tool

    def setup_tool(self, tool: str) -> dict[str, Any]:
        """The person says which tool is on the printer (Setup tab, Picker): runs TOOL_PICKER
        or TOOL_HOTEND. Only a person can say it; nothing moves."""
        self._idle()
        pc = self.m.printer
        macro = {"picker": pc.tool_picker, "hotend": pc.tool_hotend}.get(tool)
        if macro is None:
            raise ValueError("tool must be picker or hotend")
        with self._lock:
            if tool == "hotend":
                self.pins.off()
            self.printer.gcode(macro)
            now = self._ask_tool()
        if pc.tool_macro and now != tool:
            raise PrinterError(
                f"Klipper still has {now!r} mounted: is sortering_pnp.cfg included (step 1)?"
            )
        self.say("picker on the printer" if tool == "picker" else "hotend back on: sorting refused")
        return self.setup_state()

    def setup_home(self) -> dict[str, Any]:
        """Home while the user watches: Z must come down over the middle of the plate."""
        self._idle()
        layout = self.layout()
        x0, y0, x1, y1 = layout.work_rect()
        with self._lock:
            self.motion.z_home_xy = ((x0 + x1) / 2, (y0 + y1) / 2)
            try:
                self.motion.preflight(home=False)
            except PrinterError as exc:
                if "not homed" not in str(exc):
                    raise
            self.motion.home()
            self.printer.gcode("M400")
        self._setup_homed = True
        self.note("homed from the Setup tab")
        return self.setup_state()

    def setup_goto(self, target: str) -> list[float]:
        """Show a position: the photo position, two corners of the limits, the plate centre."""
        self._idle()
        pc = self.m.printer
        ox, oy, _ = self.m.tool.offset
        layout = self.layout()
        x0, y0, x1, y1 = layout.work_rect()
        spots = {
            "front_left": (pc.limits_x[0] + ox, pc.limits_y[0] + oy),
            "back_right": (pc.limits_x[1] + ox, pc.limits_y[1] + oy),
            "centre": ((x0 + x1) / 2, (y0 + y1) / 2),
        }
        with self._lock:
            self.motion.preflight(home=False)
            if target == "photo":
                self.motion.photo_position()
            elif target == "change":
                self.motion.change_position()
            elif target in spots:
                self.motion.travel(*spots[target])
                self.printer.gcode("M400")
            else:
                raise ValueError(f"unknown position {target!r}")
            return [round(v, 2) for v in self.motion.current_tip()]

    def setup_picker(self, action: str) -> dict[str, Any]:
        """Grab or release where the tool is (the Pi's pins): nothing moves."""
        self._idle()
        do = {"grab": self.pins.grab, "release": self.pins.release}.get(action)
        if do is None:
            raise ValueError("action must be grab or release")
        with self._lock:
            do()
        self._setup_picker.add(action)
        self.say(f"picker: {action}")
        return self.setup_state()

    def setup_rotate(self, angle: float | None = None) -> dict[str, Any]:
        """Tweezers: turn to `angle` (the jaws then close along it; the offset is added).
        Nothing else moves."""
        self._idle()
        pk = self.m.picker
        if not pk.kind.rotates:
            raise PrinterError(f"the {pk.kind.title.lower()} does not turn")
        a = float(angle or 0) % 180
        with self._lock:
            self.motion.turn(a + pk.rotate_offset_deg)
        self.say(f"tweezers turned to {a:.0f}°")
        self._setup_picker.add("rotate")
        return self.setup_state()

    def setup_confirm(self, key: str) -> dict[str, Any]:
        if key not in SETUP_KEYS:
            raise ValueError(f"unknown setup step {key!r}")
        if key == "homing" and not self._setup_homed:
            raise PrinterError("press Home now and watch where Z comes down first")
        if key == "machine" and not self.setup.is_done("printer"):
            raise PrinterError("check the printer first")
        if key in ("printer", "tip", "camera", "layout"):
            raise PrinterError("this step is checked by the machine, not by hand")
        if key == "picker":
            return self.setup_picker_ok()
        if key == "machine":  # the next step puts the picker on: the head goes where that is easy
            with self._lock:
                self.motion.preflight(home=False)
                self.motion.change_position()
            self.say("the head waits at the picker-change position: put the picker on")
        self.setup.mark(key)
        self.note(f"setup: {key} confirmed")
        return self.setup_state()

    def setup_picker_ok(self) -> dict[str, Any]:
        """The picker is on the head (Setup tab, Picker: OK): its pins work, and Klipper is told
        (TOOL_PICKER) so that sorting may move. Only a person can say it is on."""
        self._idle()
        ok, why = self.pins.status()
        if not ok:
            raise PrinterError(f"the picker's pins: {why}")
        if self.m.printer.tool_macro:
            self.setup_tool("picker")
        self.setup.mark("picker", self.m.picker.kind.title)
        self.note(f"setup: picker ({self.m.picker.kind.title.lower()}) is on")
        return self.setup_state()

    def setup_reset(self, key: str) -> dict[str, Any]:
        self.setup.reset(key)
        return self.setup_state()

    def _place_bins(self, layout: Layout, shot: Shot, T: Transform2D) -> list[dict[str, Any]]:
        """Move every bin whose marker is in the photo to where it is; unknown markers become new
        wild bins."""
        moved = []
        for marker, centre, yaw in self._seen_bins(layout, shot, T):
            known = layout.bin_by_marker(marker) is not None
            nb = layout.place_from_marker(marker, centre, yaw)
            moved.append({"marker": marker, "x": nb.x, "y": nb.y, "rot": nb.rot, "new": not known})
        return moved

    def _seen_bins(
        self, layout: Layout, shot: Shot, T: Transform2D
    ) -> list[tuple[int, tuple[float, float], float]]:
        """(marker, bin centre, yaw) in printer X/Y for every bin marker in the photo."""
        from .binspec import marker_offset_for, size_for_marker

        out = []
        for mk in shot.markers:
            up_bed = T.rotate_angle(mk.up_deg)
            yaw = yaw_from_up((math.cos(math.radians(up_bed)), math.sin(math.radians(up_bed))))
            b = layout.bin_by_marker(mk.marker) or next(
                (a for a in layout.away if a.marker == mk.marker), None
            )
            size = size_for_marker(mk.marker)
            own = b.own_size() if b is not None else ((size.w, size.d) if size else (1, 1))
            ox, oy = marker_offset_for(mk.marker, *own)  # shelf middle or corner post
            c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
            mx, my = (float(v) for v in T.apply(np.asarray(mk.centre)))
            out.append((mk.marker, (mx - (c * ox - s * oy), my - (s * ox + c * oy)), yaw))
        return out

    # ------------------------------------------------------------------ wild bins
    @property
    def claims_path(self) -> Path:
        p = self.m.path(self.m.paths.claims)
        assert p is not None
        return p

    def _load_volumes(self) -> CatalogueVolumes | None:
        """Exact volumes from items.json (the Files tab); None: estimated from the outlines."""
        if not self.m.run.fill.catalogue:
            return None
        path, _ = files_mod.find(self.m, "items")
        if path is None:
            return None
        try:
            cat = CatalogueVolumes.from_items(read_json(path))
        except (OSError, ValueError) as exc:
            log.warning("items.json cannot be read (%s): estimating from the outlines", exc)
            return None
        log.info("item volumes: %d items", len(cat))
        return cat or None

    # ------------------------------------------------------------------ the Files tab
    def files_state(self) -> dict[str, Any]:
        return {"slots": files_mod.slots_state(self.m)}

    def add_file(self, key: str, data: bytes, original: str = "") -> dict[str, Any]:
        store = files_mod.store_of(self.m)
        if store is None:
            raise ValueError("paths.files is not set")
        self._idle()
        info = store.save(key, data, original)
        self.note(f"file added: {files_mod.BY_KEY[key].title} ({info['summary']})")
        self._files_changed(key)
        return self.files_state()

    def remove_file(self, key: str) -> dict[str, Any]:
        store = files_mod.store_of(self.m)
        if store is not None:
            self._idle()
            store.remove(key)
            self.note(f"file removed: {files_mod.BY_KEY[key].title}")
            self._files_changed(key)
        return self.files_state()

    def _files_changed(self, key: str) -> None:
        """Use the new copy at once."""
        if key == "items":
            self.volumes = self._load_volumes()
            return
        if self.store is not None:
            self.store.classes_yaml = files_mod.find(self.m, "classes", model=False)[0]
        reload = getattr(self.vision, "reload_setup", None)
        if reload is not None:
            reload()

    def _load_claims(self) -> tuple[dict[int, str], dict[int, int], dict[int, float]]:
        """(what each wild bin collects, how many items each bin received, their volume);
        also the learnt packing and each bin's family."""
        self.packing = Packing()
        self.bin_family: dict[int, str] = {}
        self._loaded_requests: list[BinRequest] = []
        if not self.claims_path.is_file():
            return {}, {}, {}
        d = read_json(self.claims_path) or {}
        self._loaded_requests = [
            BinRequest(**{k: v for k, v in r.items() if k != "text"}) for r in d.get("requests", [])
        ]
        self.packing = Packing(
            {str(k): [float(x) for x in v] for k, v in d.get("packing", {}).items()}
        )
        self.bin_family = {int(k): str(v) for k, v in d.get("families", {}).items()}
        claims = d.get("claims", {}) if "claims" in d else d  # older files: claims only
        return (
            {int(k): str(v) for k, v in claims.items()},
            {int(k): int(v) for k, v in d.get("counts", {}).items()},
            {int(k): float(v) for k, v in d.get("volumes", {}).items()},
        )

    def _save_claims(self) -> None:
        write_json(
            self.claims_path,
            {
                "claims": {str(k): v for k, v in sorted(self.claims.items())},
                "counts": {str(k): v for k, v in sorted(self.bin_counts.items())},
                "volumes": {str(k): round(v, 1) for k, v in sorted(self.bin_volume.items())},
                "families": {str(k): v for k, v in sorted(self.bin_family.items())},
                "packing": self.packing.samples,
                "requests": [r.to_dict() for r in self.requests],
            },
        )

    def _update_ready(self, planner: Planner, layout: Layout, parts: list[Any]) -> None:
        """Mark filled bins that nothing on the plate goes to any more: they can be taken out."""
        need = planner.needed_bins(parts)
        with self._claims_lock:
            filled = {mk for mk, n in self.bin_counts.items() if n > 0} | set(self.claims)
            ready: dict[int, dict[str, Any]] = {}
            for mk in sorted(filled - need):
                b = layout.bin_by_marker(mk)
                if b is None:
                    continue
                what = self.claims.get(mk) or b.label or ", ".join(b.holds) or b.role
                ready[mk] = {"marker": mk, "what": what, "count": self.bin_counts.get(mk, 0)}
            for mk in sorted(set(ready) - set(self.ready)):
                self.note(f"bin {mk} ({ready[mk]['what']}) is complete: it can be taken out")
                self.say(f"bin {mk} complete ({ready[mk]['what']}): take it out")
                self.motion.message(f"bin {mk} ready")
            self.ready = ready

    def _target(self, plan: Plan, layout: Layout) -> dict[str, Any] | None:
        """The part picked next, for the Device tab's box: its outline, name and bin."""
        p, b = plan.part, plan.bin
        if plan.kind != "pick" or p is None:
            return None
        return {
            "id": p.id,
            "name": p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else ""),
            "poly": _poly_for_page(p.polygon),
            "xy": [round(float(v), 1) for v in p.plate_xy],
            "bin": b.marker if b is not None else None,
            "cell": layout.bin_name(b) if b is not None else None,
        }

    def ask_change(self, marker: int) -> dict[str, Any]:
        """The user wants a bin changed (its ⋯ menu): the same two steps as a full bin."""
        layout = self.layout()
        b = layout.bin_by_marker(int(marker))
        if b is None:
            raise ValueError(f"bin {marker} is not on the bed")
        with self._claims_lock:
            if not any(r.marker == b.marker for r in self.requests):
                what = self.claims.get(b.marker) or b.label or ", ".join(b.holds) or b.role
                req = BinRequest(
                    b.marker, layout.bin_name(b), "asked", what, self.bin_counts.get(b.marker, 0)
                )
                self.requests = self.requests + [req]
                self._save_claims()
                self.motion.message(req.short())
        self.note(f"bin {layout.bin_name(b)} to be changed (asked on the page)")
        return self.bins_state()

    def save_layout(self, lay: Layout) -> dict[str, Any]:
        """Layout changes from the page (queued there, applied together). While sorting they
        are used from the next pick; a layout with problems is refused then."""
        problems = lay.problems()
        running = self._thread is not None and self._thread.is_alive()
        if running and problems:
            raise ValueError("not applied while sorting: " + "; ".join(problems))
        lay.save(self.layout_path)
        if problems:
            self.setup.reset("layout")
        if running:
            self._layout_changed.set()
        self.note("layout changes saved" + (": used from the next pick" if running else ""))
        return {"layout": lay.to_dict(), "problems": problems}

    def _parked_for_bins(self) -> bool:
        """After the first OK: park (bed out, hands safe) until the second OK; True = stopped."""
        with self._lock:
            self.motion.photo_position()
            self.printer.gcode("M400")
            self.motion.message("Swap bins, Change bin")
        asked = "; ".join(r.text() for r in self.requests)
        self.note("parked for a bin change")
        self.phase("parked: change the bins, then press Change bin again")
        self._set(
            status="parked",
            message=(asked + "; " if asked else "")
            + "change the bins, then press Change bin again",
        )
        while not self._ok.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        self.swap_phase = ""
        self._set(status="running")
        return self._stop.is_set()

    def _ask_bins(self, planner: Planner, layout: Layout, parts: list[Any]) -> None:
        """Ask for bin changes: every full bin, and a bin for each new kind without one."""
        fill = self.bin_fill(layout)
        asked = {r.marker for r in self.requests}
        new: list[BinRequest] = []

        def request(mk: int, why: str, kind: str | None = None) -> BinRequest:
            b = layout.bin_by_marker(mk)
            what = self.claims.get(mk) or (b.label or ", ".join(b.holds) or b.role if b else "?")
            return BinRequest(mk, fill[mk]["cell"], why, what, fill[mk]["count"], kind)

        for mk, f in fill.items():
            if f["full"] and mk not in asked and mk != layout.work_marker:
                new.append(request(mk, "full"))
        margin = self.m.run.work_margin_mm
        on_plate = Counter(
            k
            for p in parts
            if layout.in_work(p.bed_xy, margin) and (k := wild_key(p, layout.wild_by)) is not None
        )
        for need, value in sorted(planner.needs, key=str):
            if need != "new" or any(r.for_kind == value for r in self.requests + new):
                continue
            taken = asked | {r.marker for r in new}
            share = {mk: f["share"] for mk, f in fill.items()}
            pick = choose_swap(layout, self.claims, share, on_plate, taken)
            if pick is None:  # no wild bin to give up: overflow, else wait for a bin
                planner.to_overflow.add((need, value))
                if self._no_swap_said != value:
                    has = any(b.role in ("all", "overflow") for b in layout.bins)
                    self.note(
                        f"{value} needs a bin and there is none to ask for: "
                        + ("into the overflow bin" if has else "add a bin")
                    )
                    self._no_swap_said = str(value)
                continue
            b, why = pick
            new.append(request(b.marker, "new_kind", str(value)))
            self.note(f"bin {fill[b.marker]['cell']} chosen to make room: {why}")
        if new:
            with self._claims_lock:
                self.requests = self.requests + new
                self._save_claims()
        for r in new:
            self.note(r.text())
            self.say(r.text())
            self.motion.message(r.short())

    def _resolve_requests(self, layout: Layout, shot: Shot, T: Transform2D) -> list[str]:
        """After the OK: what stands where a bin was asked for (binswap.after_ok), then every
        bin in the photo where it is."""
        seen = self._seen_bins(layout, shot, T)
        disk = self.layout()  # the whole layout: a run leaves out the bins it did not see
        out = []
        for r in list(self.requests):
            with self._claims_lock:
                self.requests = [q for q in self.requests if q is not r]
            outcome = after_ok(layout, r, seen)
            if outcome != "emptied" and disk.bin_by_marker(r.marker) is not None:
                after_ok(disk, r, seen)
            left = self._parts_in_bin(layout, r.marker, shot)
            if outcome == "emptied" and left:  # the camera still sees parts in it
                with self._claims_lock:
                    n = self.bin_counts.get(r.marker, 0)
                    each = self.bin_volume.get(r.marker, 0.0) / n if n else 0.0
                    self.bin_counts[r.marker] = left
                    self.bin_volume[r.marker] = each * left
                    self._save_claims()
                text = f"bin {r.cell} still has {left} part(s) in it: counted {left}"
            elif outcome == "emptied":
                self.bin_emptied(r.marker)
                text = f"bin {r.cell} emptied: it starts again"
            elif outcome == "replaced":
                text = (
                    f"another bin stands at {r.cell}; bin {r.marker} is kept aside with its parts"
                )
            else:
                text = f"{r.cell} is empty now; bin {r.marker} is kept aside with its parts"
            self.note(text)
            out.append(text)
        moved = self._place_bins(layout, shot, T)
        self._place_bins(disk, shot, T)
        added = [b["marker"] for b in moved if b.get("new")]
        if added:
            self.note(f"new bins seen, used as wild bins: {', '.join(map(str, added))}")
        disk.save(self.layout_path)
        with self._claims_lock:
            self._save_claims()
        return out

    def _parts_in_bin(self, layout: Layout, marker: int, shot: Shot) -> int:
        """Parts the camera sees inside a bin (its floor, without the code's shelf or post):
        a bin said to be emptied may not be. The model can miss some through the walls."""
        b = layout.bin_by_marker(marker)
        if b is None or not self.m.run.check_emptied:
            return 0
        x0, y0, x1, y1 = layout.cells_rect(b.x, b.y, b.w, b.d)
        inset = binspec.GAP_MM / 2 + binspec.WALL_MM + 1.0
        x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
        mx, my = layout.marker_centre(b)
        h = binspec.SHELF_DEPTH_MM / 2 + 1.0
        return sum(
            1
            for p in shot.parts
            if x0 <= p.bed_xy[0] <= x1
            and y0 <= p.bed_xy[1] <= y1
            and not (abs(p.bed_xy[0] - mx) <= h and abs(p.bed_xy[1] - my) <= h)
        )

    def bins_ok(self) -> dict[str, Any]:
        """The OK while not sorting: photo of the bins, then as after a bin change."""
        self._idle()
        layout, T = self.layout(), self.transform()
        with self._lock:
            self.motion.preflight(home=False)
        shot = self.take_shot(layout, T, area="bins", paint=False)
        return {"resolved": self._resolve_requests(layout, shot, T), **self.bins_state()}

    def bin_emptied(self, marker: int | None) -> dict[str, Any]:
        """The bin was taken out and emptied: forget its contents (a wild bin becomes free).
        None = every bin marked ready."""
        with self._claims_lock:
            markers = list(self.ready) if marker is None else [int(marker)]
            self.requests = [r for r in self.requests if r.marker not in markers]
            for mk in markers:
                self.claims.pop(mk, None)
                self.bin_counts.pop(mk, None)
                self.bin_volume.pop(mk, None)
                self.bin_family.pop(mk, None)
                self.ready.pop(mk, None)
            self._save_claims()
            if markers:
                self.note(f"emptied: bin {', '.join(map(str, markers))}")
            return self.bins_state()

    def bin_fill(self, layout: Layout) -> dict[int, dict[str, Any]]:
        """Every bin on the bed: where it stands, how many items went in, how full it is
        (estimated) and about how many more fit."""
        f = self.m.run.fill
        out: dict[int, dict[str, Any]] = {}
        for b in layout.bins:
            room = bin_room_mm3(*b.own_size(), b.height_u, b.marker)
            bf = BinFill(self.bin_counts.get(b.marker, 0), self.bin_volume.get(b.marker, 0.0))
            pack = self._packing(b.marker)
            out[b.marker] = {
                "cell": layout.bin_name(b),
                "count": bf.count,
                "share": round(bf.share(room, pack), 3),
                "more": bf.more(room, pack, f.default_mm3) if bf.count else None,
                "full": bf.share(room, pack) >= f.full_at,
                "packing": round(pack, 3),
            }
        return out

    def _packing(self, marker: int) -> float:
        f = self.m.run.fill
        return (
            self.packing.for_family(self.bin_family.get(marker), f.packing)
            if f.learn
            else f.packing
        )

    def bin_full(self, marker: int) -> dict[str, Any]:
        """The user says this bin is full now: learn how densely its kind packs (its items'
        volume / its room), and ask for it to be emptied."""
        layout = self.layout()
        b = layout.bin_by_marker(int(marker))
        if b is None:
            raise ValueError(f"bin {marker} is not on the bed")
        with self._claims_lock:
            vol = self.bin_volume.get(b.marker, 0.0)
            if self.m.run.fill.learn and vol > 0:
                room = bin_room_mm3(*b.own_size(), b.height_u, b.marker)
                fam = self.bin_family.get(b.marker, "")
                self.packing.add(fam, vol / room)
                self.note(
                    f"bin {layout.bin_name(b)} is full: {fam or 'its items'} pack to "
                    f"{round(100 * vol / room)} % of the room (learnt)"
                )
            self._save_claims()
        if not any(r.marker == b.marker for r in self.requests):
            fill = self.bin_fill(layout)[b.marker]
            what = self.claims.get(b.marker) or b.label or ", ".join(b.holds) or b.role
            with self._claims_lock:
                req = BinRequest(b.marker, fill["cell"], "full", what, fill["count"])
                self.requests = self.requests + [req]
                self._save_claims()
        return self.bins_state()

    def bins_state(self) -> dict[str, Any]:
        try:
            fill = {str(k): v for k, v in self.bin_fill(self.layout()).items()}
        except Exception:  # noqa: BLE001 - a broken layout file must not break the page
            fill = {}
        return {
            "claims": {str(k): v for k, v in sorted(self.claims.items())},
            "bin_counts": {str(k): v for k, v in sorted(self.bin_counts.items())},
            "ready": list(self.ready.values()),
            "fill": fill,
        }

    # ------------------------------------------------------------------ models
    def _need_store(self) -> ModelStore:
        if self.store is None:
            raise ModelError("no model store configured (paths.models)")
        return self.store

    def models_state(self) -> dict[str, Any]:
        store = self.store
        info = getattr(self.vision, "models_info", None)
        return {
            "models": [m.to_dict() for m in store.list()] if store else [],
            "active": store.active() if store else {},
            "loaded": info() if info else {"error": "simulation: no models are used"},
            "max_upload_mb": self.m.models.max_upload_mb,
        }

    def _reload_models(self) -> None:
        reload = getattr(self.vision, "reload_setup", None)
        if reload is not None:  # the parts table and class list follow the stage-1 model
            reload(strict=True)
            return
        use = getattr(self.vision, "use_models", None)
        if use is not None:
            from .vision import active_choice

            use(active_choice(self.m, self.store))

    def add_model(self, upload: Path, filename: str, name: str | None = None) -> dict[str, Any]:
        """Store an uploaded model; the first usable one of its kind becomes active."""
        store = self._need_store()
        m = store.add_upload(upload, filename, name)
        self.note(f"model uploaded: {m.name} ({m.task}, {len(m.classes)} classes)")
        role = m.role
        if role and not m.problems and store.active().get(role) is None and not self._running():
            try:
                self.activate_model(role, m.id)
            except Exception as exc:  # noqa: BLE001 - stored anyway; the page shows why
                self.note(f"{m.name} stored but not activated: {exc}")
        return m.to_dict()

    def activate_model(self, role: str, model_id: str | None) -> dict[str, Any]:
        """Switch a role to another stored model (None = no model for stage2/classifier)."""
        self._idle()
        store = self._need_store()
        if role == "stage1" and model_id is None:
            raise ModelError("stage 1 always needs a model")
        before = store.active()
        store.set_active(role, model_id)
        try:
            self._reload_models()
        except Exception:
            for r, mid in before.items():  # loading failed: back to what worked
                store.set_active(r, mid)
            raise
        name = store.get(model_id).name if model_id else "none"
        self.note(f"{role} model: {name}")
        return self.models_state()

    def test_model(self, model_id: str) -> dict[str, Any]:
        """Run a stored model on the last photo next to the active ones (nothing is switched)."""
        store = self._need_store()
        test = getattr(self.vision, "test_models", None)
        if test is None:
            raise ModelError("testing models needs the camera (not available in simulation)")
        if self.last_shot is None or self.last_shot.photo is None:
            raise ModelError("take a photo first (Device tab: One pick, or Calibrate: Take photo)")
        from .vision import active_choice

        m = store.get(model_id)
        if m.role is None:
            raise ModelError(f"{m.name}: {m.task} models cannot be used")
        try:
            choice = active_choice(self.m, store)
        except FileNotFoundError:
            if m.role != "stage1":
                raise
            choice = None
        weights = store.weights_path(m)
        if m.role == "stage1":
            choice = ModelChoice(
                weights,
                imgsz=m.imgsz,
                label=m.name,
                conf=m.recommended_conf if self.m.models.use_recommended_conf else None,
                stage2=choice.stage2 if choice else None,
                classifier=choice.classifier if choice else None,
            )
        elif choice is not None:
            setattr(choice, m.role, weights)
        assert choice is not None
        return test(choice, self.last_shot.photo, f"test_{m.id}.jpg")

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def _marker_plate_height(layout: Layout, marker: int) -> float:
    b = layout.bin_by_marker(marker)
    return layout.marker_plate_height(b.height_u if b else 3)


def _poly_for_page(poly: Any, most: int = 32) -> list[list[float]]:
    """An outline for the page: at most `most` points, mm rounded to 0.1."""
    pts = np.asarray(poly, dtype=float).reshape(-1, 2)
    if len(pts) > most:
        pts = pts[np.linspace(0, len(pts) - 1, most).astype(int)]
    return [[round(float(x), 1), round(float(y), 1)] for x, y in pts]


def _describe(plan: Plan) -> str:
    if plan.kind == "pick" and plan.part is not None and plan.bin is not None:
        p = plan.part
        name = p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")
        return f"pick {name} -> bin {plan.bin.marker}"
    return f"{plan.kind}: {plan.reason}"


def same_parts(a: list[Any], b: list[Any], tol_mm: float = 4.0, share: float = 0.8) -> bool:
    """Nothing moved between two photos (no hand in the picture, nothing still rolling)?

    Matched by place only: the model's class for a part, and whether it finds a part at all,
    may differ from one photo to the next. Still when at least `share` of the parts of each
    photo have one in the other within `tol_mm` (and at most a part or two do not)."""
    if not a or not b:
        return not a and not b
    left = list(b)
    matched = 0
    for p in a:
        hit = min(left, key=lambda q: math.dist(q.bed_xy, p.bed_xy), default=None)
        if hit is not None and math.dist(hit.bed_xy, p.bed_xy) <= tol_mm:
            left.remove(hit)
            matched += 1
    unmatched = len(a) + len(b) - 2 * matched
    return matched >= share * max(len(a), len(b)) and unmatched <= max(2, round(0.2 * len(a)))
