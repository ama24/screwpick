"""The setup checklist: what must be confirmed before the machine sorts on its own.

Until every step is done, Sort, One pick and Automatic are refused; only the setup moves are
allowed (home, jog, go to a position, the picker test, the calibration photos). Steps are
marked by hand ("it works") or automatically (tip height measured, camera calibration good
enough, bins found). Changing something they depend on un-checks them again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import read_json, write_json


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    goal: str


STEPS = (
    Step(
        "printer", "Printer", "Klipper answers, screwpick.cfg is included and the picker is mounted"
    ),
    Step("homing", "Homing", "Z homes over the middle of the plate, never over a bin"),
    Step("machine", "Machine", "Bed limits, travel height and the photo position are right"),
    Step("tip", "Tip height", "Where the picker tip touches the plate"),
    Step("picker", "Picker", "Grab and release work"),
    Step(
        "camera", "Camera to printer", "The camera's millimetres match the printer's (error < 1 mm)"
    ),
    Step(
        "layout", "Layout", "Bins and working area saved without problems, bins found by the camera"
    ),
)
KEYS = tuple(s.key for s in STEPS)


class Setup:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.done: dict[str, dict[str, Any]] = {}
        if path.is_file():
            data = read_json(path) or {}
            self.done = {k: v for k, v in data.items() if k in KEYS and isinstance(v, dict)}

    def _save(self) -> None:
        write_json(self.path, self.done)

    def mark(self, key: str, note: str = "") -> None:
        if key not in KEYS:
            raise ValueError(f"unknown setup step {key!r}")
        self.done[key] = {"at": time.strftime("%Y-%m-%d %H:%M"), "note": note}
        self._save()

    def reset(self, key: str) -> None:
        if self.done.pop(key, None) is not None:
            self._save()

    def is_done(self, key: str) -> bool:
        return key in self.done

    def missing(self) -> list[str]:
        return [s.title for s in STEPS if s.key not in self.done]

    def state(self) -> list[dict[str, Any]]:
        return [
            {
                "key": s.key,
                "title": s.title,
                "goal": s.goal,
                "done": s.key in self.done,
                "at": self.done.get(s.key, {}).get("at"),
                "note": self.done.get(s.key, {}).get("note", ""),
            }
            for s in STEPS
        ]
