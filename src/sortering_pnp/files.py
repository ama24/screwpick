"""Files from the other projects, dropped in the web page's Files tab and kept here.

sortering_pnp runs on its own: it does not reach into the other repositories' folders for its data.
Each file it can use has a slot: where to find it, what it is for, and what happens without it.
The user fetches the file and drops it in; it is checked and kept in paths.files.

Which copy is in use, first found:
  1. for the parts table and the class list: the active model's package (screw-synth puts them
     in every dataset, training-synth into every package), so they always match the model;
  2. the file dropped in the Files tab;
  3. the path set in machine.yaml (paths.calibration, ...), for whoever prefers that;
  4. the copy that comes with sortering-pnp (defaults/: vision settings, parts, classes);
  5. nothing: each slot says what is done without it.
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


def _vision(d: Any) -> str:
    if not isinstance(d, dict) or not ({"nominal_camera", "measure"} & set(d)):
        raise ValueError("not a vision.yaml (no nominal_camera or measure section)")
    h = (d.get("nominal_camera") or {}).get("height_mm")
    return f"camera at {h} mm until calibrated" if h else f"{len(d)} sections"


def _parts(d: Any) -> str:
    from .measure.geometry import check_parts

    check_parts(d)
    return f"{len(d['classes'])} classes with a length"


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
        "Made here: Calibrate tab, Camera calibration (it lands in this slot by itself).",
        "Turns pixels into millimetres on the plate, without the lens' distortion: where the "
        "parts are, and their lengths.",
        "The nominal camera: straight down from the height set in the Calibrate tab (Skip for "
        "now) or vision.yaml's; positions and lengths less exact near the picture's edge.",
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
        "vision",
        "Vision settings",
        "vision.yaml",
        "yaml",
        "Comes with sortering-pnp (src/sortering_pnp/defaults/vision.yaml): download it from here, "
        "change it, drop it back.",
        "The camera's height until it is calibrated, and how lengths are measured.",
        "The settings that come with sortering-pnp.",
        _vision,
    ),
    Slot(
        "parts",
        "Parts geometry",
        "parts.json",
        "json",
        "Inside every model package (screw-synth writes it into the dataset). Drop one here only "
        "for a model without it: screw-synth, `screwsynth-catalogue parts --out parts.json`.",
        "Per class: the standard lengths a measurement snaps to, the thread and head sizes (the "
        "grip point) and how high the axis lies.",
        "The table that comes with sortering-pnp (the full screw-synth catalogue).",
        _parts,
    ),
    Slot(
        "classes",
        "Class list",
        "classes.yaml",
        "yaml",
        "Inside every model package. Or screw-synth's configs/classes.yaml.",
        "The Models tab checks that a model knows the classes the catalogue expects.",
        "The list that comes with sortering-pnp (the full screw-synth catalogue).",
        _classes,
    ),
    Slot(
        "items",
        "Parts table (volumes)",
        "items.json",
        "json",
        "screw-synth: run `screwsynth-partgen items --out items.json` (after building "
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


DEFAULTS = Path(__file__).resolve().parent / "defaults"  # what comes with sortering-pnp
IN_MODEL = ("parts", "classes")  # slots a model package brings along

MACHINE_PATH = {
    "calibration": "calibration",
    "head_calibration": "head_calibration",
    "vision": "vision",
}


def model_file(m: Any, key: str) -> Path | None:
    """This slot's file inside the active stage-1 model's package, or None."""
    if key not in IN_MODEL:
        return None
    root = m.path(m.paths.models)
    if root is None or not root.is_dir():
        return None
    from .models import ModelStore

    sm = ModelStore(root).active_model("stage1")
    if sm is None:
        return None
    found = sorted(
        ModelStore(root).folder(sm).rglob(BY_KEY[key].filename), key=lambda p: len(p.parts)
    )
    return found[0] if found else None


def find(m: Any, key: str, model: bool = True) -> tuple[Path | None, str]:
    """(the copy in use, where it comes from: model | dropped | machine.yaml | sortering-pnp |
    none). model=False skips the model's own copy (to check a model against the catalogue)."""
    if model and (p := model_file(m, key)) is not None:
        return p, "model"
    store = store_of(m)
    if store is not None and (p := store.path(key)) is not None:
        return p, "dropped"
    attr = MACHINE_PATH.get(key)
    if attr and (p := m.path(getattr(m.paths, attr))) is not None and p.is_file():
        return p, "machine.yaml"
    p = DEFAULTS / BY_KEY[key].filename
    if p.is_file():
        return p, "sortering-pnp"
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
