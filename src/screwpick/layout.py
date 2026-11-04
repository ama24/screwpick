"""The bed layout: a Gridfinity grid with a working area (where parts are thrown and photographed)
and bins around it, each with its own marker and the rules of what it holds.

Positions are printer X/Y (the G-code frame of the bed, mm). The grid's front-left corner sits at
`origin`; cell (0, 0) is the front-left cell, x grows along printer X, y along printer Y.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import binspec
from .io import read_yaml, write_yaml

ROLES = ("wild", "sort", "unknown", "unsure", "overflow", "kit", "all")
WILD_BY = ("part", "class", "family")  # what one wild bin collects
MARKER_IDS = range(100, 250)  # DICT_4X4_250; ids below 100 are left to the calibration board


@dataclass
class Bin:
    marker: int  # its ArUco id (printed on the shelf)
    x: int  # grid cell of the front-left corner
    y: int
    w: int = 1  # size in cells along printer X
    d: int = 1  # size in cells along printer Y
    height_u: int = 3  # Gridfinity height units (7 mm)
    holds: list[str] = field(default_factory=list)  # rules: DIN912_M4x16, DIN912_M4, DIN912*, *
    role: str = "wild"  # wild | sort | unknown | unsure | overflow | kit | all
    label: str = ""

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Bin:
        b = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        b.holds = [str(h) for h in (b.holds or [])]
        return b


@dataclass
class WorkArea:
    x: int = 1
    y: int = 1
    w: int = 3
    d: int = 3


@dataclass
class Layout:
    pitch: float = binspec.PITCH_MM
    origin: list[float] = field(default_factory=lambda: [5.0, 5.0])
    cols: int = 5
    rows: int = 5
    plate_thickness_mm: float = 1.75  # TPU plate in the working area (plate frame z = 0 on top)
    bin_floor_mm: float = 0.0  # height of a bin's bottom above the bed (baseplate seat)
    wild_by: str = "part"  # a wild bin collects one: part (class + length) | class | family
    work: WorkArea = field(default_factory=WorkArea)
    bins: list[Bin] = field(default_factory=list)

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

    def work_rect(self) -> tuple[float, float, float, float]:
        return self.cells_rect(self.work.x, self.work.y, self.work.w, self.work.d)

    def in_work(self, xy: tuple[float, float], margin: float = 0.0) -> bool:
        x0, y0, x1, y1 = self.work_rect()
        return x0 + margin <= xy[0] <= x1 - margin and y0 + margin <= xy[1] <= y1 - margin

    def bin_centre(self, b: Bin) -> tuple[float, float]:
        x0, y0, x1, y1 = self.cells_rect(b.x, b.y, b.w, b.d)
        return (x0 + x1) / 2, (y0 + y1) / 2

    def bin_rim_z(self, b: Bin) -> float:
        return self.bin_floor_mm + binspec.rim_height_mm(b.height_u)

    def marker_z(self, height_u: int) -> float:
        """Printer Z of a marker's top surface on a bin of this height."""
        return self.bin_floor_mm + binspec.rim_height_mm(height_u) + binspec.MARKER_INK_MM

    def marker_plate_height(self, height_u: int) -> float:
        """The same, in the plate frame (z = 0 on the TPU), for the camera's ray intersection."""
        return self.marker_z(height_u) - self.plate_thickness_mm

    def bin_by_marker(self, marker: int) -> Bin | None:
        return next((b for b in self.bins if b.marker == marker), None)

    def next_marker(self) -> int:
        used = {b.marker for b in self.bins}
        for m in MARKER_IDS:
            if m not in used:
                return m
        raise ValueError("all marker ids are in use")

    def place_from_marker(
        self, marker: int, centre: tuple[float, float], yaw_deg: float, height_u: int | None = None
    ) -> Bin:
        """Put the bin carrying `marker` where the camera saw it: `centre` and `yaw_deg` (its +Y
        axis, printer frame) of the bin. A bin turned by 90 degrees swaps its width and depth."""
        b = self.bin_by_marker(marker)
        if b is None:
            size = binspec.size_for_marker(marker)  # printed bins: the size goes with the id
            if size is not None:
                b = Bin(marker=marker, x=0, y=0, w=size.w, d=size.d, height_u=size.height_u)
            else:
                b = Bin(marker=marker, x=0, y=0, height_u=height_u or binspec.MIN_HEIGHT_U)
            self.bins.append(b)
        quarter = round(yaw_deg / 90.0) % 2
        if quarter and b.w != b.d:
            b.w, b.d = b.d, b.w
        ox, oy = self.origin
        b.x = round((centre[0] - ox) / self.pitch - b.w / 2)
        b.y = round((centre[1] - oy) / self.pitch - b.d / 2)
        return b

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
                    f"{who}: at least {binspec.MIN_HEIGHT_U} height units (the marker shelf)"
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
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(work=work, bins=bins, **known)

    @classmethod
    def load(cls, path: Path) -> Layout:
        return cls.from_dict(read_yaml(path)) if path.is_file() else cls.default()

    def save(self, path: Path) -> None:
        write_yaml(
            path,
            self.to_dict(),
            "# screwpick bed layout (edit it in the web page's Layout tab).\n"
            "# Cells are Gridfinity units; origin = printer X/Y of the grid's front-left corner.\n",
        )

    @classmethod
    def default(cls) -> Layout:
        """5x5 grid on a 220 mm bed: a 3x3 working area in the middle, 16 bins around it:
        unknown, unsure and overflow, and 13 wild bins that sort by themselves."""
        lay = cls()
        ring = [(x, 0) for x in range(5)] + [(x, 4) for x in range(5)]
        ring += [(0, y) for y in range(1, 4)] + [(4, y) for y in range(1, 4)]
        for i, (x, y) in enumerate(ring):
            lay.bins.append(Bin(marker=MARKER_IDS.start + i, x=x, y=y))
        lay.bins[0].role, lay.bins[0].label = "unknown", "unknown parts"
        lay.bins[1].role, lay.bins[1].label = "unsure", "not sure"
        lay.bins[2].role, lay.bins[2].label = "overflow", "no bin"
        return lay


def yaw_from_up(up: tuple[float, float]) -> float:
    """Yaw of a bin (deg) from the direction its marker's top edge points to (printer frame)."""
    return math.degrees(math.atan2(up[1], up[0])) - 90.0
