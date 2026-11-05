"""Things only the user can do on the real machine, listed in the Setup tab ("On the machine").

Unlike the setup steps they do not block sorting: they are reminders. Some tick themselves off
when the program notices (a calibration was dropped, a corner bin was seen, a bin was marked
Full); the others the user ticks by hand. Hand ticks are kept in paths.todo.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import read_json, write_json


@dataclass(frozen=True)
class Todo:
    key: str
    title: str
    how: str
    auto: Callable[[Any], str | None] | None = None  # runner -> why it is done, or None


def _calibrated(r: Any) -> str | None:
    from .files import find

    key = "head_calibration" if r.m.camera.source == "head" else "calibration"
    path, source = find(r.m, key)
    return f"calibration.json {source}" if path is not None and source == "dropped" else None


def _tray_view(r: Any) -> str | None:
    return "tray view saved" if r.mask_checked else None


def _corner_bin(r: Any) -> str | None:
    seen = sorted(mk for mk in r.markers_seen if 50 <= mk < 100)
    return f"code {seen[0]} read by the camera" if seen else None


def _full_taught(r: Any) -> str | None:
    n = sum(len(v) for v in r.packing.samples.values())
    return f"learnt from {n} full bin(s)" if n else None


def _items(r: Any) -> str | None:
    return "items.json in the Files tab" if r.volumes is not None else None


def _tip(r: Any) -> str | None:
    return "tip height measured" if r.setup.is_done("tip") else None


TODOS: tuple[Todo, ...] = (
    Todo(
        "calibration",
        "Camera calibration with the board in the tray",
        "Lay the ChArUco board in the tray, at the height the parts lie, and run the "
        "screw-sorting calibration (screwsynth-calibrate) with photos from this Pi; drop the "
        "calibration.json in the Files tab.",
        _calibrated,
    ),
    Todo(
        "trayview",
        "What the model sees",
        "Calibrate tab, 'What the model sees': take a photo and check that only the tray's "
        "inside is left (adjust the margin), then Save.",
        _tray_view,
    ),
    Todo(
        "arm",
        "Camera arm measured",
        "Before printing it: with the bed all the way out, measure the reach (top bar's front "
        "face to the plate's centre) and the bar's height above the plate; Print bins tab, "
        "Holders for the top bar.",
    ),
    Todo(
        "screen",
        "Pi clear of the screen holder",
        "The Raspberry Pi on the screen's back must stay more than 12 mm inside the screen's "
        "edge (the holder's frame touches only that band).",
    ),
    Todo(
        "menu",
        "Printer menu tested",
        "On the printer's display: Screw sorter > OK (bin change) reaches the program (the "
        "console says it); Pause, Resume and Stop too.",
    ),
    Todo(
        "tool",
        "Picker: tip height and turning",
        "Setup tab: tip height; with tweezers also the 0 degree position and that the jaws turn "
        "across a part.",
        _tip,
    ),
    Todo(
        "cornerbin",
        "A corner bin printed and read",
        "Print one corner bin (codes 50-99) and Resync: the camera must read its code (cells of "
        "1.6 mm print sharp only unscaled).",
        _corner_bin,
    ),
    Todo(
        "full",
        "Full taught once",
        "When a bin is really full, press Full in its panel (Layout tab): the fill estimates "
        "learn how densely the parts pack.",
        _full_taught,
    ),
    Todo(
        "items",
        "Parts table dropped",
        "Make items.json on the PC (screwsynth-partgen items --out items.json) and drop it in "
        "the Files tab: exact volumes for the bin fill.",
        _items,
    ),
)
BY_KEY = {t.key: t for t in TODOS}


class TodoList:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _ticks(self) -> dict[str, str]:
        return read_json(self.path) if self.path.is_file() else {}

    def tick(self, key: str, done: bool) -> None:
        if key not in BY_KEY:
            raise ValueError(f"no such item {key!r}")
        ticks = self._ticks()
        if done:
            ticks[key] = time.strftime("%Y-%m-%d %H:%M")
        else:
            ticks.pop(key, None)
        write_json(self.path, ticks)

    def state(self, runner: Any) -> list[dict[str, Any]]:
        ticks = self._ticks()
        out = []
        for t in TODOS:
            seen = None
            if t.auto is not None:
                try:
                    seen = t.auto(runner)
                except Exception:  # noqa: BLE001 - a reminder must never break the page
                    seen = None
            hand = ticks.get(t.key)
            out.append(
                {
                    "key": t.key,
                    "title": t.title,
                    "how": t.how,
                    "done": bool(seen or hand),
                    "auto": t.auto is not None,
                    "why": seen or (f"ticked {hand}" if hand else ""),
                }
            )
        return out
