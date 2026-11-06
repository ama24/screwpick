"""Real-photo sessions (session.py): the folder and inventory format screw-synth reads."""

import csv

import pytest

from sortering_pnp.io import read_json
from sortering_pnp.session import (
    SessionError,
    new_session,
    read_inventory,
)

# ------------------------------------------------------------------------------- sessions


def _write_inventory(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write("# comment line\n")
        w = csv.writer(f)
        w.writerow(["part_id", "finish", "length_mm", "count", "notes"])
        for r in rows:
            w.writerow(r)


def test_session_and_inventory(tmp_path):
    d = new_session(tmp_path, "s1", operator="me", notes="first")
    assert (d / "images").is_dir() and (d / "inventory.csv").is_file()
    assert read_json(d / "session.json")["operator"] == "me"
    with pytest.raises(SessionError):
        new_session(tmp_path, "s1")  # exists
    with pytest.raises(SessionError):
        new_session(tmp_path, "bad name/")
    _write_inventory(
        d / "inventory.csv",
        [
            ["DIN912_M4x16", "black_oxide", "15.9", "3", ""],
            ["DIN912_M4x20", "black_oxide", "", "", ""],
            ["DIN934_M4", "zinc_clear", "", "5", ""],
            ["UNKNOWN", "", "", "2", "wood screws"],
        ],
    )
    rows = read_inventory(d / "inventory.csv", ["black_oxide", "zinc_clear"])
    assert [r.class_name for r in rows] == ["DIN912_M4", "DIN912_M4", "DIN934_M4", "UNKNOWN"]
    assert rows[0].length_mm == 15.9 and rows[0].count == 3
    assert rows[1].length_mm == 20.0 and rows[1].count == 1  # nominal length when no caliper value


@pytest.mark.parametrize(
    "bad",
    [
        ["DIN912_M4", "black_oxide", "x", "1", ""],
        ["NOT_A_PART", "", "", "1", ""],
        ["DIN934_M4", "gold", "", "1", ""],
    ],
)
def test_inventory_errors(tmp_path, bad):
    p = tmp_path / "inv.csv"
    _write_inventory(p, [bad])
    with pytest.raises(SessionError):
        read_inventory(p, ["black_oxide"])
