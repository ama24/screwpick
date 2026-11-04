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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .config import Machine
from .layout import Layout, yaw_from_up
from .parts import compute_gaps, to_bed
from .printer import Motion, Printer, PrinterError
from .strategies import STRATEGIES, Plan, Planner
from .transform import BedCalibration, Transform2D
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
    parts: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)


class Runner:
    def __init__(self, m: Machine, printer: Printer, vision: Vision) -> None:
        self.m = m
        self.printer = printer
        self.motion = Motion(printer, m)
        self.vision = vision
        self.state = State(strategy=m.run.strategy)
        self.events: deque[str] = deque(maxlen=300)
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
            "strategies": STRATEGIES,
            "calibrated": self.transform_path.is_file(),
            "calib_pairs": self.calib_pairs,
            "driver": self.m.printer.driver,
        }

    # ------------------------------------------------------------------ commands
    def command(self, cmd: str, strategy: str | None = None) -> str:
        cmd = (cmd or "").strip().lower()
        running = self._thread is not None and self._thread.is_alive()
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
            if running:
                self._stop.set()
                self._pause.clear()
                self._set(status="stopping", message="stopping after this move")
            return "stopping"
        if cmd in ("estop", "emergency_stop"):
            self._stop.set()
            self._pause.clear()
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
    def take_shot(self, layout: Layout, T: Transform2D | None) -> Shot:
        """Photo position, photo, parts (with printer X/Y when T is known), markers."""
        with self._lock:
            self.motion.photo_position()
            self._shot_no += 1
            name = time.strftime("%Y%m%d-%H%M%S") + f"_{self._shot_no:04d}"
            shot = self.vision.capture(name, lambda mid: _marker_plate_height(layout, mid))
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
            layout = self.layout()
            problems = layout.problems()
            if problems:
                raise PrinterError("layout: " + "; ".join(problems))
            T = self.transform()
            planner = Planner(layout, self.m, strategy)
            with self._lock:
                self.motion.preflight()
            self.note(f"started: {STRATEGIES[strategy]}")
            prev: Plan | None = None
            done_steps = 0
            while not self._stop.is_set():
                if self._pause.is_set():
                    self._set(
                        status="paused",
                        message="paused: resume from the web page or the printer menu",
                    )
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    if self._stop.is_set():
                        break
                    self._set(status="running")
                    planner.shakes_in_row = 0
                    prev = None  # someone may have moved parts
                shot = self.take_shot(layout, T)
                if prev is not None and not planner.report(prev, shot.parts):
                    self.note(
                        f"pick of {prev.part.cls if prev.part else '?'} failed: the part is still there"
                    )
                self._set(
                    counts=dict(planner.sorted_count),
                    failed_picks=planner.failed_picks,
                    picks=sum(planner.sorted_count.values()),
                )
                if steps is not None and done_steps >= steps:
                    break
                here = tuple(self.motion.current_tip()[:2])
                plan = planner.next(shot.parts, here)  # type: ignore[arg-type]
                self._set(plan=_describe(plan))
                if plan.kind == "done":
                    self._set(status="done", message=plan.reason)
                    self.note(f"done: {plan.reason}")
                    self.motion.message("screwpick: done")
                    break
                if plan.kind == "stuck":
                    self.note(plan.reason)
                    self._set(status="stuck", message=plan.reason + "; then press resume")
                    self.motion.message("screwpick: needs a hand")
                    self._pause.set()
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    planner.shakes_in_row = 0
                    prev = None
                    self._set(status="running")
                    continue
                with self._lock:
                    if plan.kind == "shake":
                        self.note(plan.reason)
                        self.motion.shake()
                    else:
                        self._pick_and_drop(plan, layout)
                prev = plan
                done_steps += 1
            if self.state.status not in ("done",):
                self._set(status="idle", message="stopped" if self._stop.is_set() else "finished")
        except Exception as exc:  # noqa: BLE001 - shown on the web page, the loop must not die silently
            log.exception("sorting stopped")
            self.note(f"error: {exc}")
            self._set(status="error", message=str(exc))

    def _pick_and_drop(self, plan: Plan, layout: Layout) -> None:
        p, b = plan.part, plan.bin
        assert p is not None and b is not None
        pk = self.m.picker
        surface = layout.plate_thickness_mm
        if pk.mode == "gripper":
            z = surface + p.top_mm * pk.grip_height
        else:
            z = surface + p.top_mm - pk.depth_mm
        z = max(z, surface + 0.3)
        name = p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")
        self.note(
            f"pick {name} at ({p.bed_xy[0]:.1f}, {p.bed_xy[1]:.1f}) -> bin {b.marker} ({plan.reason})"
        )
        self.motion.message(f"pick {name}")
        self.motion.pick(p.bed_xy[0], p.bed_xy[1], z, p.bed_angle)
        cx, cy = layout.bin_centre(b)
        self.motion.drop(cx, cy, layout.bin_rim_z(b))

    # ------------------------------------------------------------------ web-page helpers
    def _idle(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise PrinterError("the sorter is running: stop it first")

    def jog(self, dx: float, dy: float, dz: float) -> list[float]:
        self._idle()
        with self._lock:
            self.motion.preflight()
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
            self.motion.preflight()
        shot = self.take_shot(layout, cal.transform if cal else None)
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
            self.motion.preflight()
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
            self.motion.preflight()
        shot = self.take_shot(layout, T)
        from .binspec import marker_offset_mm

        moved = []
        for mk in shot.markers:
            up_bed = T.rotate_angle(mk.up_deg)
            yaw = yaw_from_up((math.cos(math.radians(up_bed)), math.sin(math.radians(up_bed))))
            b = layout.bin_by_marker(mk.marker)
            d = b.d if b is not None else 1
            ox, oy = marker_offset_mm(d)
            c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
            mx, my = (float(v) for v in T.apply(np.asarray(mk.centre)))
            centre = (mx - (c * ox - s * oy), my - (s * ox + c * oy))
            nb = layout.place_from_marker(mk.marker, centre, yaw)
            moved.append({"marker": mk.marker, "x": nb.x, "y": nb.y, "yaw": round(yaw, 1)})
        layout.save(self.layout_path)
        self.note(f"scanned bins: {len(moved)} marker(s) seen")
        return {"bins": moved, "problems": layout.problems()}


def _marker_plate_height(layout: Layout, marker: int) -> float:
    b = layout.bin_by_marker(marker)
    return layout.marker_plate_height(b.height_u if b else 3)


def _describe(plan: Plan) -> str:
    if plan.kind == "pick" and plan.part is not None and plan.bin is not None:
        p = plan.part
        name = p.cls + (f"x{p.length_mm:g}" if p.length_mm is not None else "")
        return f"pick {name} -> bin {plan.bin.marker}"
    return f"{plan.kind}: {plan.reason}"
