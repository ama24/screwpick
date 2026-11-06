"""Files from the other projects, dropped in the web page's Files tab and kept here.

sortering_pnp runs on its own: it does not reach into the other repositories' folders for its data.
Each file it can use has a slot: where to find it, what it is for, and what happens without it.
The user fetches the file and drops it in; it is checked and kept in paths.files.

Which copy is in use, first found:
  1. the file dropped in the Files tab;
  2. the path set in machine.yaml (paths.calibration, ...), for whoever prefers that;
  3. for the settings, classes and calibration only: what the installed screw-sorting code
     brings along (it is needed anyway to run the models);
  4. nothing: each slot says what is done without it.
The model itself is not here: the Models tab keeps any number of them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .io import load_yaml, read_json, write_json


@dataclass(frozen=True)
class Slot:
    key: str
    title: str
    filename: str  # kept under this name
    kind: str  # json | yaml
    where: str  # where the user finds it
    used_for: str
    without: str
    check: Callable[[Any], str]  # the parsed file -> a one-line summary; ValueError: wrong file


def _calibration(d: Any) -> str:
    if not isinstance(d, dict) or not {"camera_matrix", "dist_coeffs", "image_size"} <= set(d):
        raise ValueError("not a camera calibration (camera_matrix, dist_coeffs, image_size)")
    w, h = d["image_size"]
    plate = "with" if (d.get("extrinsic") or {}).get("T_cam_plate") else "without"
    return f"{w} x {h} px, {plate} the plate's position"


def _settings(d: Any) -> str:
    if not isinstance(d, dict) or not ({"paths", "catalogue", "measure", "calibration"} & set(d)):
        raise ValueError("not a screw-sorting config.yaml (no paths/catalogue/measure section)")
    return f"{len(d)} sections"


def _classes(d: Any) -> str:
    names = d.get("names") if isinstance(d, dict) else None
    if not isinstance(names, dict | list) or not names:
        raise ValueError("not a classes.yaml (no names)")
    return f"{len(names)} classes"


def _items(d: Any) -> str:
    if not isinstance(d, dict) or d.get("format") != "screwsynth-items":
        raise ValueError("not an items.json (made by `screwsynth-partgen items`)")
    rows = d.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("items.json lists no items")
    classes = {r.get("class") for r in rows}
    return f"{len(rows)} items in {len(classes)} classes"


SLOTS: tuple[Slot, ...] = (
    Slot(
        "calibration",
        "Camera calibration",
        "calibration.json",
        "json",
        "Made here: Calibrate tab, Camera calibration (it lands in this slot by itself). Or the "
        "screw-sorting project's data/calibration/calibration.json.",
        "Turns pixels into millimetres on the plate, without the lens' distortion: where the "
        "parts are, and their lengths.",
        "The nominal camera (straight down from the set height): positions and lengths less exact.",
        _calibration,
    ),
    Slot(
        "head_calibration",
        "Head camera calibration",
        "head_calibration.json",
        "json",
        "Made here: Calibrate tab, Camera calibration, with camera.source: head.",
        "The same, for a camera on the tool head (camera.source: head).",
        "The nominal camera. Only needed with a head camera.",
        _calibration,
    ),
    Slot(
        "settings",
        "Screw-sorting settings",
        "config.yaml",
        "yaml",
        "screw-sorting project: configs/config.yaml, or better output/<recipe>/config.yaml of the "
        "dataset the model was trained on (the same settings as its pictures).",
        "How lengths are measured, the camera's nominal height, which parts the catalogue has "
        "(head sizes for the grip point).",
        "The settings that come with the installed screw-sorting code.",
        _settings,
    ),
    Slot(
        "classes",
        "Class list",
        "classes.yaml",
        "yaml",
        "screw-sorting project: configs/classes.yaml (made by `screwsynth-catalogue classes`).",
        "The Models tab checks that a model knows the classes the catalogue expects.",
        "The list from the settings above; without that either, models are not checked (any "
        "model works, e.g. one of another project).",
        _classes,
    ),
    Slot(
        "items",
        "Parts table (volumes)",
        "items.json",
        "json",
        "screw-sorting project: run `screwsynth-partgen items --out items.json` (after building "
        "the parts); about 250 kB.",
        "The exact volume of each catalogued part, for how full the bins are and how many more "
        "fit.",
        "Each part's volume is estimated from what the camera sees (its outline times its "
        "height), and learnt from the Full button: fine for any item, a little less exact.",
        _items,
    ),
)
BY_KEY = {s.key: s for s in SLOTS}


def parse(slot: Slot, data: bytes) -> Any:
    try:
        text = data.decode("utf-8-sig")
        return json.loads(text) if slot.kind == "json" else load_yaml(text)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{slot.filename}: cannot be read ({exc})") from exc


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _meta(self) -> dict[str, Any]:
        p = self.root / "files.json"
        return read_json(p) if p.is_file() else {}

    def path(self, key: str) -> Path | None:
        """The dropped file for this slot, or None."""
        p = self.root / BY_KEY[key].filename
        return p if p.is_file() else None

    def save(self, key: str, data: bytes, original: str = "") -> dict[str, Any]:
        slot = BY_KEY.get(key)
        if slot is None:
            raise ValueError(f"no file slot {key!r}")
        summary = slot.check(parse(slot, data))
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (slot.filename + ".part")
        tmp.write_bytes(data)
        tmp.replace(self.root / slot.filename)
        meta = self._meta()
        meta[key] = {
            "original": original or slot.filename,
            "added": time.strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "bytes": len(data),
        }
        write_json(self.root / "files.json", meta)
        return meta[key]

    def remove(self, key: str) -> None:
        p = self.path(key)
        if p is not None:
            p.unlink()
        meta = self._meta()
        if meta.pop(key, None) is not None:
            write_json(self.root / "files.json", meta)

    def added(self, key: str) -> dict[str, Any] | None:
        return self._meta().get(key) if self.path(key) else None


def store_of(m: Any) -> FileStore | None:
    root = m.path(m.paths.files)
    return FileStore(root) if root is not None else None


def _screwsynth_default(key: str, settings: Path | None) -> Path | None:
    """What the installed screw-sorting code brings along (step 3), or None."""
    try:
        from screwsynth.common.config import load_config
        from screwsynth.common.paths import resolve
    except ImportError:
        return None
    try:
        if key == "settings":
            p = resolve("configs/config.yaml")
            return p if p.is_file() else None
        cfg = load_config(settings)
        if key == "calibration":
            p = resolve(
                Path(cfg.get("paths.calibration_dir", "data/calibration")) / "calibration.json"
            )
        elif key == "classes":
            p = resolve(cfg.get("paths.classes", "configs/classes.yaml"))
        else:
            return None
    except Exception:  # noqa: BLE001 - not set up: as if there were none
        return None
    return p if p.is_file() else None


MACHINE_PATH = {
    "calibration": "calibration",
    "head_calibration": "head_calibration",
    "settings": "screwsynth_config",
}


def find(m: Any, key: str) -> tuple[Path | None, str]:
    """(the copy in use, where it comes from: dropped | machine.yaml | screw-sorting | none)."""
    store = store_of(m)
    if store is not None and (p := store.path(key)) is not None:
        return p, "dropped"
    attr = MACHINE_PATH.get(key)
    if attr and (p := m.path(getattr(m.paths, attr))) is not None and p.is_file():
        return p, "machine.yaml"
    settings = find(m, "settings")[0] if key in ("calibration", "classes") else None
    p = _screwsynth_default(key, settings)
    if p is not None:
        return p, "screw-sorting"
    return None, "none"


_SUMMARIES: dict[tuple[str, str, int, int], str] = {}  # (slot, path, mtime, size) -> summary


def _summary(s: Slot, path: Path) -> str:
    """The slot's one-line summary of a file, read again only when the file changed (the
    page asks often; a big config.yaml takes a while to parse on a Pi)."""
    try:
        st = path.stat()
        key = (s.key, str(path), st.st_mtime_ns, st.st_size)
        if key not in _SUMMARIES:
            _SUMMARIES[key] = s.check(parse(s, path.read_bytes()))
        return _SUMMARIES[key]
    except (OSError, ValueError) as exc:
        return f"cannot be used: {exc}"


def slots_state(m: Any) -> list[dict[str, Any]]:
    """Every slot for the Files tab: what it is, and which copy is in use."""
    store = store_of(m)
    out = []
    for s in SLOTS:
        path, source = find(m, s.key)
        summary = _summary(s, path) if path is not None else ""
        out.append(
            {
                "key": s.key,
                "title": s.title,
                "filename": s.filename,
                "where": s.where,
                "used_for": s.used_for,
                "without": s.without,
                "source": source,
                "path": str(path) if path else None,
                "summary": summary,
                "added": store.added(s.key) if store else None,
            }
        )
    return out
