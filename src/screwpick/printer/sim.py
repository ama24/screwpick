"""A simulated printer, parts and camera: try layouts and strategies with no machine at all.

SimPrinter understands the G-code screwpick sends (G28, G90/G91, G0/G1, M400, G4, M117 and the
picker macros); SimWorld holds the parts, which the picker lifts and drops; SimVision "photographs"
the world and draws a top view for the web page.
"""

from __future__ import annotations

import math
import random
import re
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
        self.plate_thickness = layout.plate_thickness_mm
        self.layout = layout
        x0, y0, x1, y1 = layout.work_rect()
        m = 12.0
        for _ in range(n):
            cls, lengths, body, width = rng.choice(SAMPLE)
            length = float(rng.choice(lengths)) if lengths else None
            footprint = (length + width * 0.6) if length else float(body or 6.0)
            head_r = width / 2 if lengths else None
            self.parts.append(
                SimPart(
                    cls,
                    rng.uniform(x0 + m, x1 - m),
                    rng.uniform(y0 + m, y1 - m),
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
            p.x = min(max(p.x + rng.uniform(-6, 6), x0 + 8), x1 - 8)
            p.y = min(max(p.y + rng.uniform(-6, 6), y0 + 8), y1 - 8)
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

    def _tip(self) -> list[float]:
        return [p + o for p, o in zip(self.pos, self.m.tool.offset, strict=True)]

    def gcode(self, script: str) -> None:
        pk = self.m.picker
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
            elif word == pk.grab.upper():
                self.world.grab(self._tip())
            elif word == pk.release.upper():
                self.world.release(self._tip())
            elif word in ("M84", "M18"):
                self.homed = ""  # motors off: the axes are no longer homed
            elif word in ("M400", "G4", "M117", "SET_GCODE_VARIABLE") or (
                pk.rotate and word == pk.rotate.upper()
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
            tool="picker" if tool_macro else None,
        )

    def emergency_stop(self) -> None:
        self.log.append("EMERGENCY_STOP")

    def close(self) -> None:
        pass


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

    def capture(self, name: str, marker_height: Callable[[int], float]) -> Shot:
        T = self.transform()
        Ti = T.inverse()
        lay = self.layout()
        parts = []
        for i, sp in enumerate(self.world.parts, start=1):
            bed_poly = _rect(sp.x, sp.y, sp.size[0], sp.size[1], sp.angle)
            parts.append(
                Part(
                    id=i,
                    cls=sp.cls,
                    score=sp.score,
                    plate_xy=tuple(float(v) for v in Ti.apply([sp.x, sp.y])),
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
        for b in lay.bins:
            from ..binspec import marker_offset_mm

            cx, cy = lay.bin_centre(b)
            ox, oy = marker_offset_mm(b.d)
            markers.append(
                MarkerPose(
                    b.marker,
                    tuple(float(v) for v in Ti.apply([cx + ox, cy + oy])),
                    (90.0 - T.angle_deg) % 360.0,
                )
            )
        shot = Shot(name, parts, markers)
        if self.out is not None:
            shot.vis = self._draw(name, lay)
        return shot

    def _draw(self, name: str, lay: Layout) -> Path | None:
        try:
            import cv2
        except ImportError:
            return None
        assert self.out is not None
        self.out.mkdir(parents=True, exist_ok=True)
        s = 4.0  # px per mm
        W = int(lay.origin[0] * 2 + lay.cols * lay.pitch) + 1
        H = int(lay.origin[1] * 2 + lay.rows * lay.pitch) + 1
        img = np.full((int(H * s), int(W * s), 3), (243, 246, 246), np.uint8)  # BGR, light

        def px(x: float, y: float) -> tuple[int, int]:
            return int(x * s), int((H - y) * s)  # printer Y up in the picture

        x0, y0, x1, y1 = lay.work_rect()
        cv2.rectangle(img, px(x0, y0), px(x1, y1), (244, 242, 237), -1)
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
        for sp in self.world.parts:
            poly = np.array([px(*p) for p in _rect(sp.x, sp.y, sp.size[0], sp.size[1], sp.angle)])
            colour = (201, 163, 184) if sp.cls == "UNKNOWN" else (176, 169, 143)
            cv2.fillPoly(img, [poly.astype(np.int32)], colour, cv2.LINE_AA)
            cv2.putText(
                img,
                sp.cls + (f"x{sp.length:g}" if sp.length else ""),
                px(sp.x + 4, sp.y + 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (51, 49, 47),
                1,
                cv2.LINE_AA,
            )
        path = self.out / f"{name}_vis.jpg"
        cv2.imwrite(str(path), img)
        return path

    def close(self) -> None:
        pass
