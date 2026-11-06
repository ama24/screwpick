"""The bed layout: a Gridfinity grid lying anywhere on the bed, with bins on it. One bin is the
plate (role "work"): a large tray (3x3, 4x4) the parts are thrown into and photographed in;
the others hold the sorted parts, each with its own marker and the rules of what it holds.

Positions are printer X/Y (the G-code frame of the bed, mm). The grid's front-left corner sits at
`origin`; cell (0, 0) is the front-left cell, x grows along printer X, y along printer Y.
`resync` sets all of it from what the camera sees: where the grid lies, which bins stand where
and which way round, and (unless chosen by hand) the biggest bin as the plate.

Older layouts without a plate bin use `work`: cells of the grid cut out of the baseplate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import binspec
from .io import read_yaml, write_yaml

ROLES = ("wild", "sort", "work", "unknown", "unsure", "overflow", "kit", "all")
WILD_BY = ("part", "class", "family")  # what one wild bin collects
MARKER_IDS = binspec.MARKER_IDS  # 50-99: corner-post bins, 100-249: shelf bins


@dataclass
class Bin:
    marker: int  # its ArUco id (printed on the shelf)
    x: int  # grid cell of the front-left corner
    y: int
    w: int = 1  # size in cells along printer X (as it stands)
    d: int = 1  # size in cells along printer Y
    height_u: int = 3  # Gridfinity height units (7 mm)
    holds: list[str] = field(default_factory=list)  # rules: DIN912_M4x16, DIN912_M4, DIN912*, *
    role: str = "wild"  # wild | sort | work | unknown | unsure | overflow | kit | all
    label: str = ""
    rot: int = 0  # where its marker side faces: 0 back (+Y), 90 left, 180 front, 270 right

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Bin:
        b = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        b.holds = [str(h) for h in (b.holds or [])]
        b.rot = int(b.rot) % 360
        return b

    def own_size(self) -> tuple[int, int]:
        """(width, depth) in the bin's own frame (depth = towards its marker)."""
        return (self.w, self.d) if self.rot % 180 == 0 else (self.d, self.w)

    @property
    def style(self) -> str:
        """Where its marker sits: on a shelf along the back, or on a post in the back-right
        corner (the marker id says which)."""
        return binspec.style_for_marker(self.marker)


@dataclass
class WorkArea:
    x: int = 1
    y: int = 1
    w: int = 3
    d: int = 3


def _rotate(v: tuple[float, float], deg: float) -> tuple[float, float]:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return c * v[0] - s * v[1], s * v[0] + c * v[1]


@dataclass
class Layout:
    pitch: float = binspec.PITCH_MM
    origin: list[float] = field(default_factory=lambda: [5.0, 5.0])
    cols: int = 5
    rows: int = 5
    plate_thickness_mm: float = 1.75  # TPU sheet the parts lie on (0: right on the tray floor)
    bin_floor_mm: float = 0.0  # height of a bin's bottom above the bed (baseplate seat)
    wild_by: str = "part"  # a wild bin collects one: part (class + length) | class | family
    work_marker: int | None = None  # the plate: the bin the parts are thrown into
    work_auto: bool = True  # resync picks the biggest bin as the plate (False: chosen by hand)
    work: WorkArea = field(default_factory=WorkArea)  # only without a plate bin (cut-out cells)
    bins: list[Bin] = field(default_factory=list)
    away: list[Bin] = field(default_factory=list)  # bins not seen on the bed: their settings

    # ------------------------------------------------------------------ geometry (printer X/Y)
    def cells_rect(
        self, x: float, y: float, w: float, d: float
    ) -> tuple[float, float, float, float]:
        ox, oy = self.origin
        return (
            ox + x * self.pitch,
            oy + y * self.pitch,
            ox + (x + w) * self.pitch,
            oy + (y + d) * self.pitch,
        )

    def work_bin(self) -> Bin | None:
        return self.bin_by_marker(self.work_marker) if self.work_marker is not None else None

    def work_rect(self) -> tuple[float, float, float, float]:
        """Where parts lie: inside the plate bin's walls, without its marker shelf (or the
        cut-out cells of an older layout). A corner-post tray keeps all of it: only the post's
        square is left out (work_keepout)."""
        wb = self.work_bin()
        if wb is None:
            return self.cells_rect(self.work.x, self.work.y, self.work.w, self.work.d)
        x0, y0, x1, y1 = self.cells_rect(wb.x, wb.y, wb.w, wb.d)
        inset = binspec.GAP_MM / 2 + binspec.WALL_MM
        x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
        if wb.style == "corner":
            return x0, y0, x1, y1
        shelf = binspec.SHELF_DEPTH_MM
        return {
            0: (x0, y0, x1, y1 - shelf),
            90: (x0 + shelf, y0, x1, y1),
            180: (x0, y0 + shelf, x1, y1),
            270: (x0, y0, x1 - shelf, y1),
        }[wb.rot % 360]

    def surface_z(self) -> float:
        """Printer Z of the surface the parts lie on (plate frame z = 0): the tray's floor plus
        the TPU sheet, or the TPU sheet on the bed in an older layout."""
        if self.work_bin() is None:
            return self.plate_thickness_mm
        return (
            self.bin_floor_mm + binspec.BASE_HEIGHT_MM + binspec.FLOOR_MM + self.plate_thickness_mm
        )

    def work_keepout(self) -> tuple[float, float, float, float] | None:
        """The corner post inside a corner-style plate (printer X/Y), or None."""
        wb = self.work_bin()
        if wb is None or wb.style != "corner":
            return None
        mx, my = self.marker_centre(wb)
        h = binspec.POST_MM / 2
        return mx - h, my - h, mx + h, my + h

    def in_work(self, xy: tuple[float, float], margin: float = 0.0) -> bool:
        x0, y0, x1, y1 = self.work_rect()
        if not (x0 + margin <= xy[0] <= x1 - margin and y0 + margin <= xy[1] <= y1 - margin):
            return False
        post = self.work_keepout()
        if post is None:
            return True
        px0, py0, px1, py1 = post
        return not (px0 - margin < xy[0] < px1 + margin and py0 - margin < xy[1] < py1 + margin)

    def bin_centre(self, b: Bin) -> tuple[float, float]:
        x0, y0, x1, y1 = self.cells_rect(b.x, b.y, b.w, b.d)
        return (x0 + x1) / 2, (y0 + y1) / 2

    def marker_centre(self, b: Bin) -> tuple[float, float]:
        cx, cy = self.bin_centre(b)
        ox, oy = _rotate(binspec.marker_offset_for(b.marker, *b.own_size()), b.rot)
        return cx + ox, cy + oy

    def bin_rim_z(self, b: Bin) -> float:
        return self.bin_floor_mm + binspec.rim_height_mm(b.height_u)

    def marker_z(self, height_u: int) -> float:
        """Printer Z of a marker's top surface on a bin of this height."""
        return self.bin_floor_mm + binspec.rim_height_mm(height_u) + binspec.MARKER_INK_MM

    def marker_plate_height(self, height_u: int) -> float:
        """The same, in the plate frame (z = 0 where the parts lie), for the camera's rays."""
        return self.marker_z(height_u) - self.surface_z()

    def cell_name(self, x: int, y: int) -> str:
        """A cell as on a chess board: column letter from the left, row number from the front
        (A1 = front-left)."""
        col = ""
        n = x
        while True:
            col = chr(ord("A") + n % 26) + col
            n = n // 26 - 1
            if n < 0:
                break
        return f"{col}{y + 1}"

    def bin_name(self, b: Bin) -> str:
        """Where a bin stands: the cell of its front-left corner, e.g. B1."""
        return self.cell_name(b.x, b.y)

    def bin_by_marker(self, marker: int | None) -> Bin | None:
        return next((b for b in self.bins if b.marker == marker), None)

    def next_marker(self) -> int:
        used = {b.marker for b in self.bins}
        for m in [*range(100, MARKER_IDS.stop), *range(MARKER_IDS.start, 100)]:
            if m not in used:
                return m
        raise ValueError("all marker ids are in use")

    # ------------------------------------------------------------------ changes
    def set_work(self, marker: int | None, auto: bool) -> None:
        """Make `marker`'s bin the plate (None: no plate bin); the old plate becomes wild."""
        old = self.work_bin()
        if old is not None and old.marker != marker:
            old.role = "wild"
        self.work_marker = marker
        self.work_auto = auto
        new = self.work_bin()
        if new is not None:
            new.role = "work"

    def place_from_marker(
        self, marker: int, centre: tuple[float, float], yaw_deg: float, height_u: int | None = None
    ) -> Bin:
        """Put the bin carrying `marker` where the camera saw it: `centre` and `yaw_deg` (its
        +Y axis, printer frame). A bin turned by 90 degrees swaps its width and depth."""
        b = self.bin_by_marker(marker)
        if b is None:
            b = next((a for a in self.away if a.marker == marker), None)
            if b is not None:  # back on the bed: with its old rules
                self.away.remove(b)
            else:
                size = binspec.size_for_marker(marker)  # printed bins: the size goes with the id
                if size is not None:
                    b = Bin(marker=marker, x=0, y=0, w=size.w, d=size.d, height_u=size.height_u)
                else:
                    b = Bin(marker=marker, x=0, y=0, height_u=height_u or binspec.MIN_HEIGHT_U)
            self.bins.append(b)
        own_w, own_d = b.own_size()
        b.rot = (round(yaw_deg / 90.0) % 4) * 90
        b.w, b.d = (own_w, own_d) if b.rot % 180 == 0 else (own_d, own_w)
        ox, oy = self.origin
        b.x = round((centre[0] - ox) / self.pitch - b.w / 2)
        b.y = round((centre[1] - oy) / self.pitch - b.d / 2)
        return b

    def _size_seen(self, marker: int, yaw_deg: float) -> tuple[int, int]:
        """(w, d) along printer X/Y of the bin with this marker, turned by yaw."""
        b = self.bin_by_marker(marker) or next((a for a in self.away if a.marker == marker), None)
        if b is not None:
            own = b.own_size()
        else:
            size = binspec.size_for_marker(marker)
            own = (size.w, size.d) if size else (1, 1)
        return own if (round(yaw_deg / 90.0) % 2) == 0 else (own[1], own[0])

    def resync(self, seen: Sequence[tuple[int, tuple[float, float], float]]) -> dict[str, Any]:
        """Make the layout what the camera sees: `seen` = (marker, bin centre, yaw) in printer
        X/Y. Finds where the grid lies (any position on the bed), puts every bin in its cell,
        keeps the settings of bins taken away, and picks the plate: the biggest bin, unless one
        was chosen by hand and is still there."""
        if not seen:
            return {
                "found": [],
                "removed": [],
                "origin": list(self.origin),
                "work": self.work_marker,
            }
        p = self.pitch
        corners = []
        for marker, (cx, cy), yaw in seen:
            w, d = self._size_seen(marker, yaw)
            corners.append((cx - w * p / 2, cy - d * p / 2, w, d))

        def offset(values: list[float]) -> float:  # where the grid lines lie, modulo one cell
            ang = [2 * math.pi * (v % p) / p for v in values]
            m = math.atan2(sum(math.sin(a) for a in ang), sum(math.cos(a) for a in ang))
            return (m % (2 * math.pi)) * p / (2 * math.pi)

        fx, fy = offset([c[0] for c in corners]), offset([c[1] for c in corners])
        ix = [round((c[0] - fx) / p) for c in corners]
        iy = [round((c[1] - fy) / p) for c in corners]
        new_origin = [round(fx + min(ix) * p, 2), round(fy + min(iy) * p, 2)]
        cols = max(i - min(ix) + c[2] for i, c in zip(ix, corners, strict=True))
        rows = max(j - min(iy) + c[3] for j, c in zip(iy, corners, strict=True))
        same_place = all(abs(a - b) < 1.0 for a, b in zip(new_origin, self.origin, strict=True))
        self.origin = new_origin
        self.cols, self.rows = (
            (max(cols, self.cols), max(rows, self.rows)) if same_place else (cols, rows)
        )
        found = []
        for marker, centre, yaw in seen:
            b = self.place_from_marker(marker, centre, yaw)
            found.append(b.marker)
        removed = [b.marker for b in self.bins if b.marker not in found]
        self.away += [b for b in self.bins if b.marker not in found]
        self.bins = [b for b in self.bins if b.marker in found]
        chosen = self.work_bin()
        if self.work_auto or chosen is None:
            biggest = max(self.bins, key=lambda b: (b.w * b.d, -b.marker))
            self.set_work(biggest.marker, auto=True)
        return {
            "found": found,
            "removed": removed,
            "origin": list(self.origin),
            "grid": [self.cols, self.rows],
            "work": self.work_marker,
            "work_auto": self.work_auto,
        }

    # ------------------------------------------------------------------ checks
    def problems(self) -> list[str]:
        out: list[str] = []
        taken: dict[tuple[int, int], str] = {}

        def claim(x: int, y: int, w: int, d: int, who: str) -> None:
            for cx in range(x, x + w):
                for cy in range(y, y + d):
                    if not (0 <= cx < self.cols and 0 <= cy < self.rows):
                        out.append(f"{who} reaches outside the {self.cols}x{self.rows} grid")
                        return
                    if (cx, cy) in taken:
                        out.append(f"{who} overlaps {taken[(cx, cy)]} at cell ({cx}, {cy})")
                        return
                    taken[(cx, cy)] = who

        if self.wild_by not in WILD_BY:
            out.append(f"wild_by must be one of {', '.join(WILD_BY)}")
        if self.work_marker is not None:
            if self.work_bin() is None:
                out.append(f"the plate (bin {self.work_marker}) is not on the bed")
        else:
            wa = self.work
            if wa.w < 1 or wa.d < 1:
                out.append("the working area needs at least one cell")
            claim(wa.x, wa.y, wa.w, wa.d, "the working area")
        seen: set[int] = set()
        for b in self.bins:
            who = f"bin {b.marker}"
            if b.marker in seen:
                out.append(f"marker {b.marker} is used twice")
            seen.add(b.marker)
            if b.marker not in MARKER_IDS:
                out.append(f"{who}: marker ids are {MARKER_IDS.start}-{MARKER_IDS.stop - 1}")
            if b.role not in ROLES:
                out.append(f"{who}: unknown role {b.role!r} (one of {', '.join(ROLES)})")
            if b.role == "work" and b.marker != self.work_marker:
                out.append(f"{who}: only the plate has the role work")
            size = binspec.size_for_marker(b.marker)
            if size is not None and (
                sorted((b.w, b.d)) != sorted((size.w, size.d)) or b.height_u != size.height_u
            ):
                out.append(
                    f"{who}: markers {size.markers.start}-{size.markers.stop - 1} are printed on "
                    f"{size.name} bins (this one is {b.w}x{b.d}x{b.height_u}u)"
                )
            if b.height_u < binspec.MIN_HEIGHT_U:
                out.append(
                    f"{who}: at least {binspec.MIN_HEIGHT_U} height units (the marker holder)"
                )
            if b.w < 1 or b.d < 1:
                out.append(f"{who}: at least 1x1 cells")
                continue
            claim(b.x, b.y, b.w, b.d, who)
        return out

    # ------------------------------------------------------------------ files
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Layout:
        d = dict(d or {})
        work = WorkArea(**(d.pop("work", None) or {}))
        bins = [Bin.from_dict(b) for b in (d.pop("bins", None) or [])]
        away = [Bin.from_dict(b) for b in (d.pop("away", None) or [])]
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(work=work, bins=bins, away=away, **known)

    @classmethod
    def load(cls, path: Path) -> Layout:
        return cls.from_dict(read_yaml(path)) if path.is_file() else cls.default()

    def save(self, path: Path) -> None:
        write_yaml(
            path,
            self.to_dict(),
            "# screwpick bed layout (edit it on the web page's Device tab, or Resync there).\n"
            "# Cells are Gridfinity units; origin = printer X/Y of the grid's front-left corner.\n",
        )

    @classmethod
    def default(cls) -> Layout:
        """5x5 grid on a 220 mm bed: a 3x3 tray in the middle as the plate, 16 bins around it:
        unknown, unsure and overflow, and 13 wild bins that sort by themselves."""
        lay = cls()
        ring = [(x, 0) for x in range(5)] + [(x, 4) for x in range(5)]
        ring += [(0, y) for y in range(1, 4)] + [(4, y) for y in range(1, 4)]
        for i, (x, y) in enumerate(ring):
            lay.bins.append(Bin(marker=100 + i, x=x, y=y))  # shelf sorting bins
        lay.bins[0].role, lay.bins[0].label = "unknown", "unknown parts"
        lay.bins[1].role, lay.bins[1].label = "unsure", "not sure"
        lay.bins[2].role, lay.bins[2].label = "overflow", "no bin"
        tray = binspec.size_named("3x3x3u")
        lay.bins.append(Bin(marker=tray.markers.start, x=1, y=1, w=3, d=3, height_u=3))
        lay.set_work(tray.markers.start, auto=True)
        return lay


def yaw_from_up(up: tuple[float, float]) -> float:
    """Yaw of a bin (deg) from the direction its marker's top edge points to (printer frame)."""
    return math.degrees(math.atan2(up[1], up[0])) - 90.0
