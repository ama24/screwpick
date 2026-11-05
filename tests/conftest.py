from __future__ import annotations

from pathlib import Path

import pytest

from screwpick import config as config_mod
from screwpick.config import Machine
from screwpick.layout import Layout


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    m = config_mod.load(None)
    m.base_dir = tmp_path
    m.paths.layout = "layout.yaml"
    m.paths.transform = "bed_transform.json"
    m.paths.runs = "runs"
    m.paths.claims = "wild_bins.json"
    m.paths.users = "users.json"
    m.paths.models = "models"
    m.paths.state = "state.json"
    m.paths.tool = "tool.json"
    m.paths.files = "files"
    m.paths.todo = "todo.json"
    m.paths.setup = "setup.json"
    m.paths.machine = "machine.json"
    m.printer.driver = "sim"
    m.printer.photo_position = [0.0, 200.0, 60.0]
    return m


@pytest.fixture
def layout() -> Layout:
    lay = Layout.default()
    lay.bins[3].holds = ["DIN912_M3"]
    lay.bins[4].holds = ["DIN912_M4x16"]
    lay.bins[5].holds = ["DIN912_M4"]
    lay.bins[6].holds = ["DIN934*", "DIN125*"]
    lay.bins[7].holds = ["*"]
    for b in lay.bins[3:8]:
        b.role = "sort"
    return lay
