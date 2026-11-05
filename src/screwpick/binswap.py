"""Bin changes while sorting: which bin to ask for, and what the camera finds after the OK.

A bin is asked for when it is full, or when a new kind needs a bin and no wild bin is free.
For a new kind the bin to give up is chosen by a score (the highest goes):
  +2     nothing on the plate goes to it any more (its kind is done for now)
  +fill  how full it is (0..1): emptying a full bin is worth the trip
  -share the share of the parts on the plate that still go to it (likely needed again soon)

The machine never stops for it: it sorts everything else meanwhile. The user presses OK twice
(web page or printer menu): the first parks the machine (bed out), the second says the bins are
changed. A photo of the bins then tells what happened at the asked-for bin's place:
  the same code       -> the bin was emptied: its count starts again, a wild bin is free
  another code        -> a different bin stands there now; the old one is kept aside with its
                          contents (it may come back later, still full)
  no code             -> the cell is empty: the old bin is kept aside
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .layout import Bin, Layout


@dataclass
class BinRequest:
    marker: int
    cell: str  # where it stands, e.g. E1
    why: str  # full | new_kind
    what: str  # what it holds
    count: int  # parts in it
    for_kind: str | None = None  # new_kind: the kind that needs the room

    def text(self) -> str:
        holds = f"{self.what}, {self.count} part{'s' if self.count != 1 else ''}"
        if self.why == "full":
            return f"bin {self.cell} ({holds}) is full: empty it or put an empty bin there"
        return (
            f"{self.for_kind} needs a bin: empty bin {self.cell} ({holds}) "
            "or put an empty bin there"
        )

    def short(self) -> str:
        """For the printer's display (about 20 characters)."""
        return f"Bin {self.cell}: OK=park"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "text": self.text()}


def choose_swap(
    layout: Layout,
    claims: Mapping[int, str],
    fill_share: Mapping[int, float],
    on_plate: Counter,
    exclude: set[int],
) -> tuple[Bin, str] | None:
    """The wild bin to give up for a new kind, and why; None when there is none to ask for.
    `on_plate`: parts on the plate per wild key (what a wild bin collects)."""
    total = max(sum(on_plate.values()), 1)
    best: tuple[float, Bin, str] | None = None
    for b in layout.bins:
        if b.role != "wild" or b.marker not in claims or b.marker in exclude:
            continue
        key = claims[b.marker]
        left = on_plate.get(key, 0)
        share = min(fill_share.get(b.marker, 0.0), 1.0)
        score = (2.0 if left == 0 else 0.0) + share - left / total
        why = (
            "nothing on the plate goes to it"
            if left == 0
            else f"{left} on the plate still go to it"
        ) + f", about {round(share * 100)} % full"
        if best is None or score > best[0]:
            best = (score, b, why)
    return (best[1], best[2]) if best else None


def after_ok(
    layout: Layout, req: BinRequest, seen: list[tuple[int, tuple[float, float], float]]
) -> str:
    """What stands at the asked-for bin's place now (see the module text): "emptied",
    "replaced" or "removed". Changes `layout`: the old bin goes to `away` unless it is back."""
    old = layout.bin_by_marker(req.marker)
    if old is None:
        return "removed"
    x0, y0, x1, y1 = layout.cells_rect(old.x, old.y, old.w, old.d)
    if any(m == req.marker for m, _, _ in seen):
        return "emptied"
    there = [m for m, (cx, cy), _ in seen if x0 <= cx <= x1 and y0 <= cy <= y1]
    layout.bins.remove(old)
    layout.away.append(old)
    if layout.work_marker == old.marker:
        layout.set_work(None, auto=True)
    return "replaced" if there else "removed"
