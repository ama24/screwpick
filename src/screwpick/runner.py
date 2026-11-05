"""The sorting loop and everything the web page and the printer menu can ask for.

One cycle: photo position -> photo -> parts and markers -> did the last pick work? -> plan ->
pick and drop (or shake) -> again. Commands (start, pause, resume, stop, step) arrive from the web
page or from the printer's menu and are handled between moves, never in the middle of one: a
part in the picker is always dropped first.
"""

from __future__ import annotations

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

from . import binspec
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
from .printer import Motion, Printer, PrinterError
from .rules import family, wild_key
from .setup import KEYS as SETUP_KEYS
from .setup import Setup
from .strategies import STRATEGIES, Plan, Planner
from .todo import TodoList
from .transform import BedCalibration, Transform2D
from .trayview import WorkArea
from .trayview import rect as tray_rect
from .vision import Shot, Vision

log = logging.getLogger("screwpick.runner")


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


class Runner:
    def __init__(
        self, m: Machine, printer: Printer, vision: Vision, store: ModelStore | None = None
    ) -> None:
        self.m = m
        self.printer = printer
        self.motion = Motion(printer, m)
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
        self._park = threading.Event()  # first OK, while sorting: park after this move
        self._ok = threading.Event()  # second OK: the bins are changed
        self.swap_phase = ""  # "" sorting | parked (waiting for the second OK)
        self._no_swap_said = ""
        self.mask_checked = False  # the tray view was checked and saved (Calibrate tab)
        self.markers_seen: set[int] = set()  # every code the camera has read
        self._load_tool()
        self._load_machine()
        setup_path = self.m.path(self.m.paths.setup)
        assert setup_path is not None
        self.setup = Setup(setup_path)
        todo_path = self.m.path(self.m.paths.todo)
        assert todo_path is not None
        self.todo = TodoList(todo_path)
        # the simulator needs no setup; a real printer does
        self.require_setup = self.m.printer.driver != "sim"
        self._setup_homed = False  # homed from the Setup tab since the program started
        self._setup_picker: set[str] = set()  # grab / release tried
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
        return Layout.load(self.layout_path)

    def calibration(self) -> BedCalibration | None:
        return BedCalibration.load(self.transform_path)

    def transform(self) -> Transform2D:
        cal = self.calibration()
        if cal is not None:
            return cal.transform
        if self.m.printer.driver != "sim":
            raise PrinterError("the camera is not calibrated to the printer yet: Calibrate tab")
        return Transform2D.identity()

    # ------------------------------------------------------------------ status
    def note(self, text: str) -> None:
        log.info(text)
        self.events.append(time.strftime("%H:%M:%S ") + text)

    def say(self, text: str) -> None:
        """One short line on the page's console: what the machine sees and decides."""
        self.console.append(time.strftime("%H:%M:%S ") + text)

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
                if self.state.status in ("paused", "stuck") and cmd == "start":
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
        if cmd == "ok":  # first OK: park; second OK: the bins are changed, check them
            if self.swap_phase != "parked":
                self.swap_phase = "parked"
                if running:
                    self._park.set()
                    return "OK: parking after this move; change the bins, then OK again"
                self.note("parked for a bin change")
                return "OK: change the bins, then OK again"
            if running:
                self._ok.set()
                return "OK: checking the bins, then sorting on"
            self.swap_phase = ""
            res = self.bins_ok()
            return "OK: " + ("; ".join(res["resolved"]) or "nothing was asked for")
        if cmd == "pause":
            if running:
                self._pause.set()
                self._set(status="paused", message="pausing after this move")
            return "paused"
        if cmd == "resume":
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
                self.motion.photo_position()
            name = self._photo_name()
            shot = self.vision.capture(name, lambda mid: _marker_plate_height(layout, mid), work)
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
        """A photo painted with this margin (not saved): the Run tab's picture shows what the
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

    def mask_save(self, margin_mm: float) -> dict[str, Any]:
        p = self.m.path(self.m.paths.machine)
        assert p is not None
        kept = read_json(p) if p.is_file() else {}
        write_json(p, {**kept, "mask_margin_mm": float(margin_mm)})
        self.m.camera.mask_margin_mm = float(margin_mm)
        self.mask_checked = True
        self.note(f"tray view saved: margin {float(margin_mm):g} mm")
        return {"margin_mm": float(margin_mm), "saved": True}

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
            parts=[
                {
                    "cls": p.cls,
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
        layout, T = self._prepare()
        with self._lock:
            self.motion.preflight()
            if self.m.printer.accel:
                self.printer.gcode(f"SET_VELOCITY_LIMIT ACCEL={self.m.printer.accel:.0f}")
            if self.m.picker.rotate_home:  # the tweezers' 0 deg (lost when the motors were off)
                self.printer.gcode(self.m.picker.rotate_home)
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
            self._set(plan=_describe(plan))
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
                    self.motion.message("Bin change: OK=park")
                self._set(status="waiting", message=asked + "; press OK to park")
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
                self.motion.message("screwpick: done")
                return "done"
            if plan.kind == "stuck":
                self.note(plan.reason)
                self.say("stuck: " + plan.reason)
                self.motion.message("screwpick: needs a hand")
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
    def _wait(self, seconds: float) -> bool:
        """Sleep, waking up for stop; True when stopped."""
        return self._stop.wait(max(0.0, seconds))

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
        sb = self.m.standby
        tried: list[Any] | None = None  # the parts of a stuck plate: wait until they change
        errors = 0
        self.note(f"automatic mode on: {STRATEGIES[strategy]}")
        self.say("automatic: waiting for parts")
        while not self._stop.is_set():
            try:
                if self._pause.is_set():
                    self._set(status="paused", message="automatic mode paused")
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    tried = None
                    continue
                _, parts, _ = self._look()
                if len(parts) >= sb.min_parts and not (
                    tried is not None and same_parts(parts, tried)
                ):
                    self.say(f"{len(parts)} part(s) seen: waiting until they lie still")
                    self._set(
                        status="standby",
                        message=f"{len(parts)} part(s) seen: waiting until they lie still",
                    )
                    if self._wait(sb.settle_s):
                        break
                    _, again, _ = self._look()
                    if same_parts(parts, again):
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
                self._set(status=status, message=f"{msg} · next look at {nxt}")
                if self._wait(sb.interval_s):
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

    def _load_tool(self) -> None:
        p = self.m.path(self.m.paths.tool)
        if p is not None and p.is_file():
            d = read_json(p) or {}
            if "offset_z" in d:
                self.m.tool.offset[2] = float(d["offset_z"])

    def tip_touches_plate(self) -> dict[str, Any]:
        """The tip was jogged down until it just touches the TPU plate: from now on that is the
        plate's height, whatever Z the homing gives (see "Homing" in the README)."""
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
            self.motion.photo_position()
            name = self._photo_name()
            photo = self.vision.snap(name)
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
        if pk.mode in ("gripper", "tweezers"):
            h = p.grip_height_mm if p.grip_height_mm is not None else p.top_mm * pk.grip_height
            z = surface + h
        else:
            z = surface + p.top_mm - pk.depth_mm
        z = max(z, surface + 0.3)
        x, y = p.grip_bed if p.grip_method != "centre" else p.bed_xy
        jaws = jaw_angle(p.bed_angle, pk.rotate_offset_deg)
        name = p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")
        self.note(f"pick {name} at ({x:.1f}, {y:.1f}) -> bin {b.marker} ({plan.reason})")
        turn = (
            f", jaws {jaws:.0f}°"
            if pk.rotate and pk.mode in ("gripper", "tweezers", "both")
            else ""
        )
        self.say(f"pick {name} at X{x:.0f} Y{y:.0f}{turn} → bin {b.marker}")
        self.motion.message(f"pick {name}")
        self.motion.pick(x, y, z, jaws)

    def _drop(self, plan: Plan, layout: Layout) -> None:
        b = plan.bin
        assert b is not None
        cx, cy = layout.bin_centre(b)
        self.motion.drop(cx, cy, layout.bin_rim_z(b))
        self.say(f"dropped in bin {b.marker}")

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
        shot = self.take_shot(layout, cal.transform if cal else None, area="here")
        out = []
        for mk in shot.markers:
            d: dict[str, Any] = {"marker": mk.marker, "plate": [round(v, 2) for v in mk.centre]}
            if cal is not None:
                d["bed"] = [round(float(v), 2) for v in cal.transform.apply(mk.centre)]
            out.append(d)
        return out

    def calib_record(self, marker: int) -> dict[str, Any]:
        """Pair the camera's view of `marker` with where the tool tip is now (jogged onto it)."""
        if self.last_shot is None:
            raise PrinterError("take a calibration photo first")
        mk = next((x for x in self.last_shot.markers if x.marker == marker), None)
        if mk is None:
            raise PrinterError(f"marker {marker} was not in the last photo")
        tip = self.tip()
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
        cal = BedCalibration.fit(self.calib_pairs)
        cal.save(self.transform_path)
        worst = max(cal.residuals_mm)
        self.note(f"calibrated: {len(self.calib_pairs)} points, worst error {worst:.2f} mm")
        if worst < 1.0:
            self.setup.mark("camera", f"{len(self.calib_pairs)} points, worst {worst:.2f} mm")
        else:
            self.setup.reset("camera")
        return {
            "residuals_mm": cal.residuals_mm,
            "angle_deg": round(cal.transform.angle_deg, 3),
            "scale": round(cal.transform.scale, 5),
        }

    def calib_clear(self) -> None:
        self.calib_pairs = []

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
    MACHINE_KEYS = ("photo_position", "travel_z", "limits_x", "limits_y", "limits_z")

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
                if len(vals) != (3 if k == "photo_position" else 2):
                    raise ValueError(f"{k}: wrong number of values")
                if k != "photo_position" and not vals[0] < vals[1]:
                    raise ValueError(f"{k}: the minimum must be below the maximum")
                new[k] = vals
        old = self.machine_values()
        for k, v in new.items():
            setattr(self.m.printer, k, v)
        try:
            x, y, z = new["photo_position"]
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
            "rotation": bool(self.m.picker.rotate),
        }

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
                if pc.tool_macro:
                    checks.append(
                        {
                            "what": "screwpick.cfg is included",
                            "ok": st.tool is not None,
                            "detail": "[include screwpick.cfg] in printer.cfg",
                        }
                    )
                    checks.append(
                        {
                            "what": "the picker is mounted (TOOL_PICKER)",
                            "ok": st.tool == "picker",
                            "detail": f"active tool: {st.tool}",
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
            elif target in spots:
                self.motion.travel(*spots[target])
                self.printer.gcode("M400")
            else:
                raise ValueError(f"unknown position {target!r}")
            return [round(v, 2) for v in self.motion.current_tip()]

    def setup_picker(self, action: str) -> dict[str, Any]:
        """Run the grab or release macro where the tool is: nothing moves."""
        self._idle()
        pk = self.m.picker
        macro = {"grab": pk.grab, "release": pk.release}.get(action)
        if macro is None:
            raise ValueError("action must be grab or release")
        with self._lock:
            self.printer.gcode(macro)
        self._setup_picker.add(action)
        return self.setup_state()

    def setup_rotate(self, angle: float | None = None, zero: bool = False) -> dict[str, Any]:
        """Tweezers: turn to `angle` (the jaws then close along it), or call the present
        position 0 degrees (the jaws close along printer X). Nothing else moves."""
        self._idle()
        pk = self.m.picker
        if not pk.rotate:
            raise PrinterError("no rotation axis configured (picker.rotate)")
        with self._lock:
            if zero:
                macro = pk.rotate_home or pk.rotate_zero
                if not macro:
                    raise PrinterError("no zero macro configured (picker.rotate_zero)")
                self.printer.gcode(macro)
                self.say("tweezers: this is 0°")
            else:
                self.printer.gcode(f"{pk.rotate} ANGLE={float(angle or 0) % 180:.1f}")
                self.say(f"tweezers turned to {float(angle or 0) % 180:.0f}°")
        self._setup_picker.add("zero" if zero else "rotate")
        return self.setup_state()

    def setup_confirm(self, key: str) -> dict[str, Any]:
        if key not in SETUP_KEYS:
            raise ValueError(f"unknown setup step {key!r}")
        if key == "homing" and not self._setup_homed:
            raise PrinterError("press Home now and watch where Z comes down first")
        if key == "machine" and not self.setup.is_done("printer"):
            raise PrinterError("check the printer first")
        if key == "picker" and not {"grab", "release"} <= self._setup_picker:
            raise PrinterError("try Grab and Release first")
        if key == "picker" and self.m.picker.rotate and "rotate" not in self._setup_picker:
            raise PrinterError("turn the tweezers to 0° and 90° first")
        if key in ("printer", "tip", "camera", "layout"):
            raise PrinterError("this step is checked by the machine, not by hand")
        self.setup.mark(key)
        self.note(f"setup: {key} confirmed")
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
            self.store.classes_yaml = files_mod.find(self.m, "classes")[0]
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

    def _parked_for_bins(self) -> bool:
        """After the first OK: park (bed out, hands safe) until the second OK; True = stopped."""
        with self._lock:
            self.motion.photo_position()
            self.printer.gcode("M400")
            self.motion.message("Change bins, OK")
        asked = "; ".join(r.text() for r in self.requests)
        self.note("parked for a bin change")
        self.say("parked: change the bins, then OK")
        self._set(
            status="parked",
            message=(asked + "; " if asked else "") + "change the bins, then press OK again",
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
            raise ModelError("take a photo first (Run tab: One pick, or Calibrate: Take photo)")
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


def _describe(plan: Plan) -> str:
    if plan.kind == "pick" and plan.part is not None and plan.bin is not None:
        p = plan.part
        name = p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")
        return f"pick {name} -> bin {plan.bin.marker}"
    return f"{plan.kind}: {plan.reason}"


def same_parts(a: list[Any], b: list[Any], tol_mm: float = 3.0) -> bool:
    """The same parts at the same places (nothing moved, no hand in the picture)?"""
    if len(a) != len(b):
        return False
    left = list(b)
    for p in a:
        hit = next(
            (q for q in left if q.cls == p.cls and math.dist(q.bed_xy, p.bed_xy) <= tol_mm), None
        )
        if hit is None:
            return False
        left.remove(hit)
    return True
