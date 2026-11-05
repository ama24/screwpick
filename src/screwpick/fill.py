"""How full a bin is: the volume of the items that went in, against the room inside the bin.

An item's volume, best first:
- the catalogue: items.json (dropped in the Files tab, made by `screwsynth-partgen items`)
  has the exact mesh volume of every catalogued item; by part number, else by class at the
  measured length, else the class average;
- generic, for any item the model segments (screws today, anything tomorrow): from what the
  camera measured, its outline area on the plate times its height, times a shape factor (a
  lying screw fills about half of that box: round shank, the head only at one end).

Loose items never fill a bin solid: the packing is the share of the room they really occupy.
It starts at run.fill.packing and is learnt from every bin marked full by hand (Packing), per
family: washers pack differently from long screws.

A bin's room comes from its Gridfinity size: the inside from the floor to the rim, without the
marker shelf or corner post. So a 1x2 bin holds twice a 1x1, and a 6-unit bin a little over
twice a 3-unit one (the floor is the same).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import binspec


@dataclass
class Fill:
    shape: float = 0.5  # item volume = outline area x height x this
    packing: float = 0.3  # loose items fill this share of a bin's room
    full_at: float = 0.9  # a bin this full (estimated) asks to be emptied
    default_mm3: float = 300.0  # an item without an outline (nothing measured)
    catalogue: bool = True  # exact volumes from items.json (Files tab), when there is one
    learn: bool = True  # "Full" on a bin teaches how densely its kind really packs


def polygon_area(poly: np.ndarray) -> float:
    if len(poly) < 3:
        return 0.0
    x, y = poly[:, 0], poly[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def item_volume_mm3(part, fill: Fill) -> float:  # noqa: ANN001 - parts.Part
    """Estimated volume of one item from its segmentation outline and its height."""
    area = polygon_area(np.asarray(part.polygon, dtype=float))
    if area <= 0 or not part.top_mm or part.top_mm <= 0:
        return fill.default_mm3
    return area * float(part.top_mm) * fill.shape


def bin_room_mm3(w: int, d: int, height_u: int, marker: int | None = None) -> float:
    """The room inside a bin (w x d cells, height_u units) from the floor to the rim."""
    wall = binspec.WALL_MM
    iw, idp = binspec.outer_mm(w) - 2 * wall, binspec.outer_mm(d) - 2 * wall
    h = binspec.rim_height_mm(height_u) - binspec.BASE_HEIGHT_MM - binspec.FLOOR_MM
    room = iw * idp * h
    if marker is not None and binspec.style_for_marker(marker) == "corner":
        room -= binspec.POST_MM**2 * h
    else:  # the shelf: its triangle under the top, along the whole width
        s = binspec.SHELF_DEPTH_MM
        room -= iw * (s * 1.6 + min(s, h) ** 2 / 2)
    return max(room, 1.0)


@dataclass
class BinFill:
    """What went into one bin since it was last emptied."""

    count: int = 0
    volume_mm3: float = 0.0  # the items' own volume (estimated)

    def share(self, room_mm3: float, packing: float) -> float:
        """How full: 0 empty, 1 full (as far as loose items can fill it)."""
        return self.volume_mm3 / (room_mm3 * packing)

    def more(self, room_mm3: float, packing: float, default_mm3: float = 300.0) -> int:
        """About how many more items like these fit."""
        each = self.volume_mm3 / self.count if self.count else default_mm3
        left = room_mm3 * packing - self.volume_mm3
        return max(0, int(left / max(each, 1.0)))


class CatalogueVolumes:
    """Exact volumes of the catalogued items, from items.json (the Files tab). Better than the
    outline estimate when the item is known: by part number, else by its class (at the
    measured length, or the class average)."""

    def __init__(self, by_class: dict[str, list[tuple[float | None, float]]] | None = None):
        self.by_class = by_class or {}  # class -> [(length or None, volume mm3)]

    @classmethod
    def from_items(cls, table: dict) -> CatalogueVolumes:
        """From an items.json (screwsynth-partgen items), dropped in the Files tab."""
        by: dict[str, list[tuple[float | None, float]]] = {}
        for r in table.get("items", []):
            try:
                vol = float(r["volume_mm3"])
                name = str(r["class"])
            except (ValueError, KeyError, TypeError):
                continue
            if name.upper() == "UNKNOWN" or vol <= 0:
                continue  # every unknown item is different: the outline knows better
            length = r.get("length")
            by.setdefault(name, []).append((float(length) if length else None, vol))
        for v in by.values():
            v.sort(key=lambda t: (t[0] is None, t[0] or 0.0))
        return cls(by)

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_class.values())

    def volume(self, cls: str, length: float | None) -> float | None:
        """mm3 of one item of this class (and length), None when not catalogued."""
        rows = self.by_class.get(cls)
        if not rows:
            return None
        sized = [(n, v) for n, v in rows if n is not None]
        if length is None or not sized:
            return float(np.mean([v for _, v in rows]))
        for (n0, v0), (n1, v1) in zip(sized, sized[1:], strict=False):  # between two lengths
            if n0 <= length <= n1:
                return v0 + (v1 - v0) * (length - n0) / (n1 - n0) if n1 > n0 else v0
        n, v = min(sized, key=lambda t: abs(t[0] - length))  # beyond the ends: scale
        return v * length / n if n > 0 else v


def part_volume_mm3(part, fill: Fill, catalogue: CatalogueVolumes | None = None) -> float:  # noqa: ANN001
    """The best volume we have for one item: the catalogue's, else the outline estimate."""
    if catalogue is not None and not part.unknown:
        v = catalogue.volume(part.cls, part.length_mm)
        if v is not None:
            return v
    return item_volume_mm3(part, fill)


@dataclass
class Packing:
    """How densely loose items really fill a bin, learnt each time a bin is marked full by
    hand (its items' volume / its room). Kept per family (washers lie flatter than long
    screws); a family without samples uses all of them, and none at all the setting."""

    samples: dict[str, list[float]] = field(default_factory=dict)  # family -> shares
    keep: int = 10

    def add(self, family: str, share: float) -> None:
        s = self.samples.setdefault(family, [])
        s.append(round(min(max(share, 0.02), 0.95), 4))
        del s[: -self.keep]

    def for_family(self, family: str | None, default: float) -> float:
        own = self.samples.get(family or "")
        if own:
            return float(np.median(own))
        every = [x for v in self.samples.values() for x in v]
        return float(np.median(every)) if every else default
