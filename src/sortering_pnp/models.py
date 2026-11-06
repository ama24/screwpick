"""The model store: models uploaded through the web page, kept side by side, one active per role.

Upload a training-synth package (zip of the package folder: best.pt, model_card.json, exports/…),
a zipped NCNN export folder, or a single .onnx / .pt file. Each upload becomes a folder in the
store with its own sortering_pnp.json; older models stay until deleted, so going back is one click.

Roles follow the model's YOLO task:
  stage1      segment (full: outlines and lengths), obb or detect (positions and classes only)
  stage2      pose: keypoints for the length measurement (optional)
  classifier  classify: re-checks the class of every part on a close-up crop (optional)
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .io import read_json, read_yaml, write_json

log = logging.getLogger("sortering_pnp.models")

TASK_ROLE = {
    "segment": "stage1",
    "detect": "stage1",
    "obb": "stage1",
    "pose": "stage2",
    "classify": "classifier",
}
ROLES = ("stage1", "stage2", "classifier", "head")  # head: stage 1 for the head camera
WEIGHT_SUFFIXES = (".pt", ".onnx")
MAX_UNZIPPED = 4 * 1024**3
META = "sortering_pnp.json"


class ModelError(ValueError):
    pass


@dataclass
class StoredModel:
    id: str
    name: str
    uploaded: str
    source: str  # the uploaded file name
    task: str  # segment | detect | obb | pose | classify | unknown
    formats: list[dict[str, str]] = field(default_factory=list)  # {"format", "path"}
    weights: str | None = None  # the chosen entry of formats (path relative to the model folder)
    classes: list[str] = field(default_factory=list)
    imgsz: int | None = None
    metric: str | None = None  # e.g. "mask mAP50-95"
    metric_value: float | None = None
    recommended_conf: float | None = None
    has_card: bool = False
    problems: list[str] = field(default_factory=list)  # it cannot be used
    warnings: list[str] = field(default_factory=list)  # it can, but read this

    @property
    def role(self) -> str | None:
        return TASK_ROLE.get(self.task)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "role": self.role}


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return s[:40] or "model"


def _format_of(p: Path) -> str | None:
    if p.is_dir() and (p / "model.ncnn.param").is_file():
        return "ncnn"
    if p.is_file() and p.suffix.lower() in WEIGHT_SUFFIXES:
        return p.suffix.lower()[1:]
    return None


def find_formats(folder: Path) -> list[dict[str, str]]:
    """Every usable weights file or export folder under `folder`."""
    out = []
    for p in sorted(folder.rglob("*")):
        fmt = _format_of(p)
        if fmt is None or any(_format_of(a) == "ncnn" for a in p.parents if folder in a.parents):
            continue
        out.append({"format": fmt, "path": p.relative_to(folder).as_posix()})
    return out


def pick_weights(formats: list[dict[str, str]], prefer: tuple[str, ...]) -> str | None:
    """The preferred format (NCNN first on the Pi); full precision before half before int8."""

    def rank(f: dict[str, str]) -> tuple[int, int, str]:
        p = f["path"].lower()
        prec = 2 if "int8" in p else 1 if ("fp16" in p or "half" in p) else 0
        order = prefer.index(f["format"]) if f["format"] in prefer else len(prefer)
        return order, prec, p

    return min(formats, key=rank)["path"] if formats else None


def _names_list(names: Any) -> list[str]:
    if isinstance(names, dict):
        return [str(names[k]) for k in sorted(names, key=lambda k: int(k))]
    return [str(n) for n in (names or [])]


def _imgsz(v: Any) -> int | None:
    if isinstance(v, (list, tuple)) and v:
        return int(max(int(x) for x in v))
    return int(v) if v else None


def _safe_extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        total = 0
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise ModelError(f"unsafe path in the zip: {info.filename}")
            total += info.file_size
            if total > MAX_UNZIPPED:
                raise ModelError("the zip unpacks to more than 4 GB")
        z.extractall(dest)


class ModelStore:
    def __init__(
        self,
        root: Path,
        classes_yaml: Path | None = None,
        prefer: tuple[str, ...] = ("ncnn", "onnx", "pt"),
        inspect: Callable[[Path], tuple[str, list[str], int | None]] | None = None,
    ) -> None:
        self.root = root
        self.classes_yaml = classes_yaml
        self.prefer = tuple(prefer)
        self.inspect = inspect or inspect_with_ultralytics

    # ------------------------------------------------------------------ listing
    def list(self) -> list[StoredModel]:
        if not self.root.is_dir():
            return []
        out = []
        for meta in sorted(self.root.glob(f"*/{META}")):
            try:
                out.append(StoredModel(**read_json(meta)))
            except (TypeError, ValueError) as exc:
                log.warning("%s: %s", meta, exc)
        return sorted(out, key=lambda m: m.uploaded, reverse=True)

    def get(self, model_id: str) -> StoredModel:
        p = self.root / _slug(model_id) / META
        if not p.is_file():
            raise ModelError(f"no model {model_id!r}")
        return StoredModel(**read_json(p))

    def folder(self, m: StoredModel) -> Path:
        return self.root / m.id

    def weights_path(self, m: StoredModel) -> Path:
        if not m.weights:
            raise ModelError(f"{m.name}: no usable weights")
        return self.folder(m) / m.weights

    # ------------------------------------------------------------------ active models
    def active(self) -> dict[str, str | None]:
        p = self.root / "active.json"
        d = read_json(p) if p.is_file() else {}
        return {r: d.get(r) for r in ROLES}

    def set_active(self, role: str, model_id: str | None) -> dict[str, str | None]:
        if role not in ROLES:
            raise ModelError(f"unknown role {role!r}")
        if model_id is not None:
            m = self.get(model_id)
            if m.role != role and not (role == "head" and m.role == "stage1"):
                raise ModelError(f"{m.name} is a {m.task} model: it cannot be the {role} model")
            if m.problems:
                raise ModelError(f"{m.name} cannot be used: {'; '.join(m.problems)}")
        act = self.active()
        act[role] = model_id
        write_json(self.root / "active.json", act)
        return act

    def active_model(self, role: str) -> StoredModel | None:
        mid = self.active().get(role)
        if not mid:
            return None
        try:
            return self.get(mid)
        except ModelError:
            return None

    # ------------------------------------------------------------------ changes
    def add_upload(self, upload: Path, filename: str, name: str | None = None) -> StoredModel:
        """Turn an uploaded file into a stored model (and delete the upload)."""
        stem = Path(filename).name
        base = _slug(name or re.sub(r"\.(zip|pt|onnx)$", "", stem, flags=re.I))
        model_id = time.strftime("%Y%m%d-%H%M%S") + "_" + base
        while (self.root / model_id).exists():
            model_id += "x"
        folder = self.root / model_id
        folder.mkdir(parents=True)
        try:
            suffix = Path(stem).suffix.lower()
            if suffix == ".zip":  # not is_zipfile(): a .pt is a zip archive too
                if not zipfile.is_zipfile(upload):
                    raise ModelError(f"{stem} is not a valid zip file")
                _safe_extract(upload, folder)
            elif suffix in WEIGHT_SUFFIXES:
                shutil.move(str(upload), folder / stem)
            else:
                raise ModelError(
                    f"{stem}: expected a .zip (model package or NCNN folder), .onnx or .pt"
                )
            m = self._describe(folder, model_id, name or base, stem)
            write_json(folder / META, asdict(m))
            return m
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            upload.unlink(missing_ok=True)

    def choose_weights(self, model_id: str, path: str) -> StoredModel:
        m = self.get(model_id)
        if path not in {f["path"] for f in m.formats}:
            raise ModelError(f"{m.name} has no {path}")
        m.weights = path
        write_json(self.folder(m) / META, asdict(m))
        return m

    def rename(self, model_id: str, name: str) -> StoredModel:
        m = self.get(model_id)
        m.name = name.strip() or m.name
        write_json(self.folder(m) / META, asdict(m))
        return m

    def delete(self, model_id: str) -> None:
        m = self.get(model_id)
        if model_id in self.active().values():
            raise ModelError(f"{m.name} is in use: choose another model first")
        shutil.rmtree(self.folder(m))

    # ------------------------------------------------------------------ inspection
    def _describe(self, folder: Path, model_id: str, name: str, source: str) -> StoredModel:
        formats = find_formats(folder)
        if not formats:
            raise ModelError(
                "no model inside: expected best.pt, a .onnx file or an NCNN folder "
                "(model.ncnn.param + model.ncnn.bin)"
            )
        m = StoredModel(
            id=model_id,
            name=name,
            uploaded=time.strftime("%Y-%m-%d %H:%M:%S"),
            source=source,
            task="unknown",
            formats=formats,
            weights=pick_weights(formats, self.prefer),
        )
        cards = sorted(folder.rglob("model_card.json"), key=lambda p: len(p.parts))
        card: dict[str, Any] = read_json(cards[0]) if cards else {}
        m.has_card = bool(card)
        meta: dict[str, Any] = {}
        for f in formats:  # Ultralytics writes metadata.yaml into every NCNN export
            if f["format"] == "ncnn" and (folder / f["path"] / "metadata.yaml").is_file():
                meta = read_yaml(folder / f["path"] / "metadata.yaml") or {}
                break
        task = card.get("task") or meta.get("task")
        classes = list(card.get("classes") or []) or _names_list(meta.get("names"))
        imgsz = _imgsz(card.get("imgsz") or meta.get("imgsz"))
        if not task or not classes:
            try:
                task2, classes2, imgsz2 = self.inspect(self.weights_path(m))
                task, classes, imgsz = task or task2, classes or classes2, imgsz or imgsz2
            except Exception as exc:  # noqa: BLE001 - shown on the page
                m.warnings.append(f"could not open the weights to read their task ({exc})")
        m.task = str(task or "unknown")
        m.classes = classes
        m.imgsz = imgsz
        metrics = card.get("metrics") or {}
        m.metric = metrics.get("main_metric")
        m.metric_value = metrics.get("main_value")
        m.recommended_conf = card.get("recommended_conf")
        mismatch = card.get("classes") and meta.get("names")
        if mismatch and list(card["classes"]) != _names_list(meta["names"]):
            m.problems.append("model_card.json and the weights list different classes")
        if m.role is None:
            m.problems.append(
                f"task {m.task!r}: sortering_pnp uses segment, detect, obb, pose or classify"
            )
        if m.task in ("detect", "obb"):
            m.warnings.append(
                "no outlines: parts are found and sorted by class, lengths are not measured"
            )
        if not m.has_card:
            m.warnings.append(
                "no model_card.json: scores and the recommended confidence are unknown"
            )
        if m.role in ("stage1", "classifier"):
            warnings = self._class_warnings(m.classes)
            if warnings and warnings[0].startswith("none of"):
                m.problems.append(warnings.pop(0))
            m.warnings += warnings
        if not any(f["format"] == "ncnn" for f in formats):
            m.warnings.append(
                "no NCNN export: slower on a Raspberry Pi (export NCNN in training-synth)"
            )
        return m

    def _class_warnings(self, classes: list[str]) -> list[str]:
        if not self.classes_yaml or not self.classes_yaml.is_file():
            return []
        expected = set((read_yaml(self.classes_yaml) or {}).get("names", {}).values())
        if not expected:
            return []
        out = []
        if classes and not set(classes) & expected:
            return ["none of its classes are in the catalogue: a model of another project?"]
        unknown = sorted(set(classes) - expected)
        missing = sorted(expected - set(classes))
        if unknown:
            out.append(
                f"{len(unknown)} class(es) the catalogue does not know: {', '.join(unknown[:6])}"
            )
        if missing:
            out.append(
                f"{len(missing)} catalogue class(es) not in this model: {', '.join(missing[:6])}"
            )
        return out


def make_store(m: Any) -> ModelStore | None:
    """The store of a Machine config (paths.models), checking classes against screwsynth's
    classes.yaml when screwsynth is installed."""
    root = m.path(m.paths.models)
    if root is None:
        return None
    from .files import find

    classes_yaml, _ = find(m, "classes")  # the Files tab's copy first
    if classes_yaml is None:
        log.info("no class list (Files tab): uploaded models are not checked")
    return ModelStore(root, classes_yaml, tuple(m.models.prefer))


def inspect_with_ultralytics(weights: Path) -> tuple[str, list[str], int | None]:
    """Open the weights with Ultralytics to read task, classes and image size (slow; only used
    when there is no model card or NCNN metadata). Only upload models you trust: .pt files are
    Python pickles."""
    from ultralytics import YOLO

    y = YOLO(str(weights))
    args = getattr(getattr(y, "model", None), "args", {}) or {}
    imgsz = args.get("imgsz") if isinstance(args, dict) else None
    return str(y.task), _names_list(y.names), _imgsz(imgsz)
