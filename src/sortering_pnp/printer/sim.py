"""A simulated printer, parts and camera: try layouts and strategies with no machine at all.

SimPrinter understands the G-code sortering_pnp sends (G28, G90/G91, G0/G1, M400, G4, M117,
SET_VELOCITY_LIMIT and the picker macros); SimWorld holds the parts, which the picker lifts and
drops; SimVision "photographs" the world and draws a top view for the web page.
"""

from __future__ import annotations

import copy
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import Machine
from ..layout import Layout
from ..markers import MarkerPose
from ..parts import Part
from ..transform import Transform2D
from ..vision import Shot, top_height_mm
from .base import PrinterError, PrinterStatus

SAMPLE = [  # class, length options (None: no length), body length, width
    ("DIN912_M3", [8, 10, 12, 16], None, 5.5),
    ("DIN912_M4", [10, 16, 20], None, 7.0),
    ("DIN933_M5", [12, 16, 20], None, 8.0),
    ("ISO7380_M4", [8, 10, 12], None, 7.6),
    ("DIN934_M4", None, 7.0, 7.0),
    ("DIN125A_M4", None, 9.0, 9.0),
    ("UNKNOWN", None, 14.0, 6.0),
]


@dataclass
class SimPart:
    cls: str
    x: float
    y: float
    angle: float
    length: float | None
    size: tuple[float, float]  # footprint length x width, mm
    top: float
    score: float = 0.92


@dataclass
class SimWorld:
    parts: list[SimPart] = field(default_factory=list)
    held: SimPart | None = None
    dropped: list[tuple[SimPart, tuple[float, float]]] = field(default_factory=list)
    plate_thickness: float = 1.75
    grab_radius_mm: float = 3.0
    seed: int = 0
    layout: Layout | None = None

    def populate(self, layout: Layout, n: int = 12, seed: int | None = None) -> None:
        rng = random.Random(self.seed if seed is None else seed)
        self.parts.clear()
        self.dropped.clear()
        self.plate_thickness = layout.surface_z()  # where the parts lie (printer Z)
        self.layout = layout
        x0, y0, x1, y1 = layout.work_rect()
        m = 12.0
        for _ in range(n):
            cls, lengths, body, width = rng.choice(SAMPLE)
            while True:  # not on a corner-style plate's post
                x, y = rng.uniform(x0 + m, x1 - m), rng.uniform(y0 + m, y1 - m)
                if layout.in_work((x, y), m / 2):
                    break
            length = float(rng.choice(lengths)) if lengths else None
            footprint = (length + width * 0.6) if length else float(body or 6.0)
            head_r = width / 2 if lengths else None
            self.parts.append(
                SimPart(
                    cls,
                    x,
                    y,
                    rng.uniform(0, 180),
                    length,
                    (footprint, width if lengths else float(body or 6.0)),
                    top_height_mm(cls, head_r),
                )
            )

    def grab(self, tip: list[float]) -> None:
        best = min(self.parts, key=lambda p: math.hypot(p.x - tip[0], p.y - tip[1]), default=None)
        if best is None or self.held is not None:
            return
        reach = self.plate_thickness + best.top + 1.0
        if math.hypot(best.x - tip[0], best.y - tip[1]) <= self.grab_radius_mm and tip[2] <= reach:
            self.parts.remove(best)
            self.held = best

    def release(self, tip: list[float]) -> None:
        if self.held is not None:
            self.dropped.append((self.held, (tip[0], tip[1])))
            self.held = None

    def shake(self, rng: random.Random) -> None:
        if self.layout is None:
            return
        x0, y0, x1, y1 = self.layout.work_rect()
        for p in self.parts:
            x = min(max(p.x + rng.uniform(-6, 6), x0 + 8), x1 - 8)
            y = min(max(p.y + rng.uniform(-6, 6), y0 + 8), y1 - 8)
            if self.layout.in_work((x, y), 4.0):  # a corner post stops it
                p.x, p.y = x, y
            p.angle = (p.angle + rng.uniform(-30, 30)) % 180


class SimPrinter:
    def __init__(self, m: Machine, world: SimWorld) -> None:
        self.m = m
        self.world = world
        self.pos = [0.0, 0.0, 0.0]
        self.homed = ""
        self.relative = False
        self.log: list[str] = []
        self.rng = random.Random(1)
        self.jaw_angle: float | None = None  # last tweezer rotation (SimPins)
        self.tool = "picker"  # TOOL_PICKER / TOOL_HOTEND
        self.accel = 3000.0  # SET_VELOCITY_LIMIT ACCEL= (Klipper's max_accel)

    def _tip(self) -> list[float]:
        return [p + o for p, o in zip(self.pos, self.m.tool.offset, strict=True)]

    def gcode(self, script: str) -> None:
        shaking = 0
        for raw in script.splitlines():
            line = raw.split(";")[0].strip()
            if not line:
                continue
            self.log.append(line)
            word = line.split()[0].upper()
            if word == "G28":
                self.pos, self.homed = [0.0, 0.0, 0.0], "xyz"
            elif word == "G90":
                self.relative = False
            elif word == "G91":
                self.relative = True
            elif word in ("G0", "G1"):
                if not self.homed:
                    raise PrinterError("Must home axis first")
                for axis, val in re.findall(r"([XYZ])(-?\d+(?:\.\d+)?)", line.upper()):
                    i = "XYZ".index(axis)
                    self.pos[i] = self.pos[i] + float(val) if self.relative else float(val)
                if self.relative:
                    shaking += 1
            elif word == self.m.printer.tool_picker.upper():
                self.tool = "picker"
            elif word == self.m.printer.tool_hotend.upper():
                self.tool = "hotend"
            elif word == "SET_VELOCITY_LIMIT":
                m = re.search(r"ACCEL=(\d+(?:\.\d+)?)", line.upper())
                self.accel = float(m.group(1)) if m else self.accel
            elif word in ("M84", "M18"):
                self.homed = ""  # motors off: the axes are no longer homed
            elif self.m.picker.rotate_macro and word == self.m.picker.rotate_macro.upper():
                m = re.search(r"ANGLE=(-?\d+(?:\.\d+)?)", line.upper())  # tweezers on a stepper
                self.jaw_angle = float(m.group(1)) if m else self.jaw_angle
            elif word in ("M400", "G4", "M117", "SET_GCODE_VARIABLE") or (
                self.m.picker.rotate_home and word == self.m.picker.rotate_home.upper()
            ):
                pass
            else:
                raise PrinterError(f"simulated printer: unknown command {line!r}")
        if shaking >= 2:
            self.world.shake(self.rng)

    def status(self, tool_macro: str | None = None) -> PrinterStatus:
        return PrinterStatus(
            state="ready",
            homed=self.homed,
            position=list(self.pos),
            hotend_c=24.0,
            tool=self.tool if tool_macro else None,
            max_accel=self.accel,
        )

    def emergency_stop(self) -> None:
        self.log.append("EMERGENCY_STOP")

    def close(self) -> None:
        pass


class SimPins:
    """The picker's pins in the simulator: the simulated parts are held and let go at the tip.
    Each action is written to the printer's log ("; pins grab") so tests can follow the order."""

    def __init__(self, printer: SimPrinter) -> None:
        self.printer = printer
        self.ok, self.why = True, "simulated pins"
        self.pump_on = False

    def grab(self) -> None:
        self.printer.log.append("; pins grab")
        self.pump_on = self.printer.m.picker.kind.pump
        self.printer.world.grab(self.printer._tip())

    def release(self) -> None:
        self.printer.log.append("; pins release")
        self.pump_on = False
        self.printer.world.release(self.printer._tip())

    def turn(self, angle_deg: float) -> None:
        self.printer.log.append(f"; pins turn {angle_deg:.1f}")
        self.printer.jaw_angle = round(angle_deg % 180.0, 1)

    def off(self) -> None:
        self.printer.log.append("; pins off")
        self.pump_on = False

    def status(self) -> tuple[bool, str]:
        return self.ok, self.why

    def close(self) -> None:
        self.off()


def _rect(cx: float, cy: float, length: float, width: float, angle_deg: float) -> np.ndarray:
    a = math.radians(angle_deg)
    u = np.array([math.cos(a), math.sin(a)]) * length / 2
    v = np.array([-math.sin(a), math.cos(a)]) * width / 2
    c = np.array([cx, cy])
    return np.array([c - u - v, c + u - v, c + u + v, c - u + v])


class SimVision:
    def __init__(
        self,
        world: SimWorld,
        layout: Callable[[], Layout],
        transform: Callable[[], Transform2D],
        out_dir: Path | None = None,
    ) -> None:
        self.world = world
        self.layout = layout
        self.transform = transform
        self.out = out_dir
        self.snaps: list[SimPart | None] = []
        # a head camera: () -> the tip X/Y now; it sees fov_mm around tip + offset
        self.head_tip: Callable[[], list[float]] | None = None
        self.head = None  # config.HeadCamera
        self.head_ref: tuple[float, float] = (0.0, 0.0)  # tip X/Y of the calibration photo
        self.painted: list[bool] = []  # per photo: was everything outside the tray painted over
        self._px: np.ndarray | None = None  # bed mm -> picture pixels of the last drawing

    delay_s: float = 0.0  # pretend the analysis takes this long (time to act while it sorts)

    def capture(self, name: str, marker_height: Callable[[int], float], work=None) -> Shot:  # noqa: ANN001
        return self.analyse(self.snap(name), name, marker_height, work)

    def snap(self, name: str) -> list[SimPart]:
        """The plate as it is now (the part in the picker is not on it)."""
        self.snaps.append(self.world.held)  # what the picker holds at the moment of the photo
        return [copy.copy(sp) for sp in self.world.parts]

    def analyse(
        self,
        plate: list[SimPart],
        name: str,
        marker_height: Callable[[int], float],
        work=None,  # noqa: ANN001 - trayview.WorkArea: the model sees only the tray's inside
    ) -> Shot:
        if self.delay_s:
            time.sleep(self.delay_s)
        T = self.transform()
        Ti = T.inverse()
        lay = self.layout()
        d = np.zeros(2)  # the head moved by d from the calibration photo: the view shifts
        seen = lambda x, y: True  # noqa: E731

        if self.head_tip is not None and self.head is not None:
            tip = np.asarray(self.head_tip()[:2], dtype=float)
            d = tip - np.asarray(self.head_ref, dtype=float)
            cx, cy = tip + np.asarray(self.head.offset, dtype=float)
            hw, hh = self.head.fov_mm[0] / 2, self.head.fov_mm[1] / 2

            def seen(x: float, y: float) -> bool:
                return abs(x - cx) <= hw and abs(y - cy) <= hh

        plate = [sp for sp in plate if seen(sp.x, sp.y)]
        if work is not None:  # painted over: the model cannot see what lies outside the tray
            plate = [
                sp for sp in plate if work.contains(tuple(Ti.apply([sp.x - d[0], sp.y - d[1]])))
            ]
        self.painted.append(work is not None)
        parts = []
        for i, sp in enumerate(plate, start=1):
            bed_poly = _rect(sp.x - d[0], sp.y - d[1], sp.size[0], sp.size[1], sp.angle)
            parts.append(
                Part(
                    id=i,
                    cls=sp.cls,
                    score=sp.score,
                    plate_xy=tuple(float(v) for v in Ti.apply([sp.x - d[0], sp.y - d[1]])),
                    plate_angle=(sp.angle - T.angle_deg) % 180.0,
                    polygon=Ti.apply(bed_poly),
                    top_mm=sp.top,
                    length_mm=sp.length,
                    measurable=True,
                    unknown=sp.cls == "UNKNOWN",
                    has_length=sp.length is not None,
                )
            )
        markers = []
        for b in lay.bins:  # each code on its bin's shelf or post, its top edge to the back
            mx, my = lay.marker_centre(b)
            if not seen(mx, my):
                continue
            markers.append(
                MarkerPose(
                    b.marker,
                    tuple(float(v) for v in Ti.apply([mx - d[0], my - d[1]])),
                    (90.0 + b.rot - T.angle_deg) % 360.0,
                )
            )
        shot = Shot(name, parts, markers)
        if self.out is not None:
            painted = [T.apply(work.outline)] if work is not None else None
            shot.vis = self._draw(name, lay, plate, painted)  # what the model saw
            shot.view = self._draw(name, lay, plate, None, "view")  # the whole picture
            if self._px is not None:  # plate -> bed (T) -> picture pixels
                Tm = np.eye(3)
                Tm[:2, :2], Tm[:2, 2] = T.A, T.t
                shot.view_h = (self._px @ Tm).tolist()
        return shot

    def _draw(
        self,
        name: str,
        lay: Layout,
        plate: list[SimPart],
        keep: list[np.ndarray] | None = None,  # painted: only this (bed mm) is left
        suffix: str = "vis",
    ) -> Path | None:
        try:
            import cv2
        except ImportError:
            return None
        assert self.out is not None
        self.out.mkdir(parents=True, exist_ok=True)
        s = 4.0  # px per mm
        W = int(lay.origin[0] * 2 + lay.cols * lay.pitch) + 1
        H = int(lay.origin[1] * 2 + lay.rows * lay.pitch) + 1
        self._px = np.array([[s, 0.0, 0.0], [0.0, -s, H * s], [0.0, 0.0, 1.0]])  # bed -> px
        img = np.full((int(H * s), int(W * s), 3), (243, 246, 246), np.uint8)  # BGR, light

        def px(x: float, y: float) -> tuple[int, int]:
            return int(x * s), int((H - y) * s)  # printer Y up in the picture

        for b in lay.bins:
            bx0, by0, bx1, by1 = lay.cells_rect(b.x, b.y, b.w, b.d)
            cv2.rectangle(img, px(bx0 + 2, by0 + 2), px(bx1 - 2, by1 - 2), (255, 255, 255), -1)
            cv2.rectangle(img, px(bx0 + 2, by0 + 2), px(bx1 - 2, by1 - 2), (210, 216, 217), 1)
            n = sum(
                1 for _, xy in self.world.dropped if bx0 <= xy[0] <= bx1 and by0 <= xy[1] <= by1
            )
            cv2.putText(
                img,
                f"{b.marker}:{n}",
                px(bx0 + 3, by1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (141, 138, 135),
                1,
                cv2.LINE_AA,
            )
        x0, y0, x1, y1 = lay.work_rect()
        cv2.rectangle(img, px(x0, y0), px(x1, y1), (244, 242, 237), -1)
        post = lay.work_keepout()
        if post is not None:
            cv2.rectangle(img, px(post[0], post[1]), px(post[2], post[3]), (210, 216, 217), -1)
        for sp in plate:
            poly = np.array([px(*p) for p in _rect(sp.x, sp.y, sp.size[0], sp.size[1], sp.angle)])
            colour = (150, 140, 160) if sp.cls == "UNKNOWN" else (150, 152, 148)  # bare metal
            cv2.fillPoly(img, [poly.astype(np.int32)], colour, cv2.LINE_AA)
        if keep is not None:  # what the model sees: the rest painted the floor's colour
            mask = np.zeros(img.shape[:2], np.uint8)
            for poly in keep:
                cv2.fillPoly(mask, [np.array([px(*q) for q in poly], np.int32)], 255)
            img[mask == 0] = (244, 242, 237)
        path = self.out / f"{name}_{suffix}.jpg"
        cv2.imwrite(str(path), img)
        return path

    def close(self) -> None:
        pass
