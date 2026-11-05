"""machine.yaml: how this printer, its picker and the sorting run are set up.

Every section is a dataclass with defaults, so a config file only needs the keys that differ.
Relative paths are resolved against the folder of the config file. Lengths in mm, speeds in
mm/min, angles in degrees.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .fill import Fill

PICKER_MODES = ("vacuum", "gripper", "tweezers", "both")
PRINTER_DRIVERS = ("moonraker", "sim")


class ConfigError(ValueError):
    pass


@dataclass
class Paths:
    screwsynth_config: str | None = None  # screwsynth's configs/config.yaml (classes, geometry)
    calibration: str | None = None  # calibration.json of the camera; None = nominal camera
    models: str = "../models"  # models uploaded in the web page (Models tab)
    model: str | None = None  # fallback when no model is active: weights or NCNN folder
    stage2: str | None = None  # keypoint model; None = classical measurement only
    layout: str = "../data/layout.yaml"  # bins and working area (edited in the web page)
    transform: str = "../data/bed_transform.json"  # plate mm -> printer X/Y (calibrate tab)
    runs: str = "../runs"  # photos and results of every run
    claims: str = "../data/wild_bins.json"  # what each wild bin collects (kept across runs)
    users: str = "../data/users.json"  # web page accounts (create the first one in the page)
    state: str = "../data/state.json"  # automatic mode on/off (it survives restarts)
    tool: str = "../data/tool.json"  # tip height measured in the Calibrate tab
    setup: str = "../data/setup.json"  # the Setup tab's checklist
    machine: str = "../data/machine.json"  # limits/positions set in the Setup tab (over this file)


@dataclass
class Camera:
    settings: str | None = (
        None  # screwrig lock settings (locked focus/exposure); None = rpicam-still
    )


@dataclass
class Printer:
    driver: str = "moonraker"  # moonraker (Klipper) | sim (no printer: a simulated one)
    url: str = "http://localhost:7125"  # Moonraker on this Pi
    # tip X Y Z for photos and standby: bed all the way out (Y max on a bed-slinger), head out
    # of the picture; the camera calibration is made with the bed here
    photo_position: list[float] = field(default_factory=lambda: [0.0, 220.0, 60.0])
    travel_z: float = 50.0  # tip height for every move (above the bins)
    feed_xy: float = 6000.0
    feed_z: float = 1200.0
    limits_x: list[float] = field(default_factory=lambda: [0.0, 220.0])
    limits_y: list[float] = field(default_factory=lambda: [0.0, 220.0])
    limits_z: list[float] = field(default_factory=lambda: [0.0, 120.0])
    max_hotend_c: float = 50.0  # refuse to run while the hotend is hotter than this
    auto_home: bool = True  # home (G28) when an axis is not homed
    # homing: Z is homed over the middle of the working area (flat plate, no bins) through
    # this macro's variables z_home_x / z_home_y (klipper/screwpick.cfg); None = plain G28
    home_macro: str | None = "_SCREWPICK_HOME"
    tool_macro: str | None = "_SCREWPICK_TOOL"  # its variable `tool` must be "picker"; None = skip
    timeout_s: float = 120.0
    # mm/s^2 while sorting (gentler bed moves: loose parts stay where the photo saw them);
    # None = the printer's own setting
    accel: float | None = None


@dataclass
class Tool:
    offset: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # tip - toolhead, X Y Z


@dataclass
class Picker:
    mode: str = "vacuum"  # vacuum | gripper | tweezers (turned by a Z rotation axis) | both
    grab: str = "SCREWPICK_GRAB"  # Klipper macros, see klipper/screwpick.cfg
    release: str = "SCREWPICK_RELEASE"
    rotate: str | None = None  # SCREWPICK_ROTATE ANGLE=<deg> when the gripper can turn
    rotate_home: str | None = None  # SCREWPICK_ROT_HOME: finds 0 deg (jaws close along X)
    rotate_zero: str | None = "SCREWPICK_ROT_ZERO"  # Setup tab: the jaws close along X now
    rotate_offset_deg: float = 0.0  # fine trim of the jaw angle
    grip_from_head_mm: float = 1.5  # tweezers close at least this far below a screw head
    approach_mm: float = 8.0  # slow descent from this far above the part
    depth_mm: float = 0.5  # go this far below the top of the part (spring-loaded cup)
    grip_height: float = 0.5  # gripper: close at this share of the part height (0 bed, 1 top)
    clearance_mm: float = 1.0  # free space a part needs around it to be picked first
    drop_mm: float = 10.0  # release this far above the bin's rim
    dwell_s: float = 0.3  # wait after grab / release
    feed_z_slow: float = 300.0


@dataclass
class Shake:
    enabled: bool = True  # spread a clump by shaking the bed when nothing can be picked
    axis: str = "Y"
    moves: int = 6
    distance_mm: float = 4.0
    feed: float = 9000.0
    max_in_a_row: int = 3  # then pause and ask for a hand


@dataclass
class Run:
    strategy: str = "sort"  # sort | clear | kit
    min_score: float = 0.5  # below: the "unsure" bin
    kit: dict[str, int] = field(default_factory=dict)  # kit strategy: rule -> how many
    max_failed_picks: int = 3  # the same spot fails this often: leave the part, warn
    failed_radius_mm: float = 4.0  # a part still within this of a pick: the pick failed
    work_margin_mm: float = 3.0  # parts closer than this to the working-area wall are skipped
    keep_photos: int = 300  # keep this many photos in the runs folder (0 = keep all)
    scan_bins_on_start: bool = True  # find the bins with the camera when a run starts
    only_seen_bins: bool = True  # use only the bins the camera sees (covered ones are skipped)
    # census: the most numerous kind first, finished before the next, and back to a kind
    # done earlier when more of it turns up | largest_group: the biggest on each photo | nearest
    order: str = "census"
    # photo right after the pick (the part in the picker), analysed while it is carried to
    # its bin: the Pi's thinking time hides behind the move
    pipeline: bool = True
    shake: Shake = field(default_factory=Shake)
    fill: Fill = field(default_factory=Fill)  # how full the bins are (estimated, see fill.py)


@dataclass
class Standby:
    interval_s: float = 60.0  # automatic mode: look for new parts this often
    settle_s: float = 8.0  # parts must lie still this long before sorting (a hand still throwing)
    min_parts: int = 1  # start sorting from this many parts
    motors_off: bool = True  # motors off while waiting (quiet; homes again before sorting)
    max_errors: int = 3  # automatic mode switches itself off after this many errors in a row


@dataclass
class Models:
    classifier_conf: float = 0.6  # the classifier overrules stage 1 only when at least this sure
    use_recommended_conf: bool = True  # stage-1 confidence from the model card, when it has one
    prefer: list[str] = field(default_factory=lambda: ["ncnn", "onnx", "pt"])  # weights to use
    max_upload_mb: int = 2000


@dataclass
class Web:
    port: int = 8080
    host: str = ""  # "" = every network interface (open it from the PC)
    require_login: bool = True  # user name and password for the page (False: open to the network)
    session_days: int = 30  # stay logged in this long on a browser


@dataclass
class Machine:
    paths: Paths = field(default_factory=Paths)
    camera: Camera = field(default_factory=Camera)
    printer: Printer = field(default_factory=Printer)
    tool: Tool = field(default_factory=Tool)
    picker: Picker = field(default_factory=Picker)
    run: Run = field(default_factory=Run)
    models: Models = field(default_factory=Models)
    standby: Standby = field(default_factory=Standby)
    web: Web = field(default_factory=Web)
    base_dir: Path = field(default_factory=Path.cwd)  # folder of the config file

    def path(self, value: str | None) -> Path | None:
        """A configured path, relative ones against the config file's folder."""
        if value in (None, ""):
            return None
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def validate(self) -> None:
        if self.picker.mode not in PICKER_MODES:
            raise ConfigError(
                f"picker.mode must be one of {PICKER_MODES}, got {self.picker.mode!r}"
            )
        if self.printer.driver not in PRINTER_DRIVERS:
            raise ConfigError(
                f"printer.driver must be one of {PRINTER_DRIVERS}, got {self.printer.driver!r}"
            )
        for name in ("photo_position", "limits_x", "limits_y", "limits_z"):
            v = getattr(self.printer, name)
            if len(v) != (3 if name == "photo_position" else 2):
                raise ConfigError(f"printer.{name}: wrong number of values {v}")
        if len(self.tool.offset) != 3:
            raise ConfigError("tool.offset needs X Y Z")
        if self.run.order not in ("census", "largest_group", "nearest"):
            raise ConfigError("run.order must be census, largest_group or nearest")
        if self.run.shake.axis.upper() not in ("X", "Y"):
            raise ConfigError("run.shake.axis must be X or Y")


def _build(cls: type, data: Mapping[str, Any] | None, where: str) -> Any:
    data = dict(data or {})
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    names = {f.name for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in names or key == "base_dir":
            raise ConfigError(f"{where}: unknown key {key!r} (known: {sorted(names)})")
        hint = hints[key]
        if dataclasses.is_dataclass(hint):
            kwargs[key] = _build(hint, value, f"{where}.{key}")
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load(path: str | Path | None = None) -> Machine:
    """Read machine.yaml (or defaults when `path` is None)."""
    if path is None:
        m = Machine()
    else:
        p = Path(path).expanduser().resolve()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{p}: expected a mapping at the top")
        m = _build(Machine, raw, p.name)
        m.base_dir = p.parent
    m.validate()
    return m
