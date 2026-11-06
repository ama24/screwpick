"""torch imports once per process: the first real error is kept and shown, not the later
"`torch` can only be initialized once per process"."""

from __future__ import annotations

import sys

import pytest

from sortering_pnp import yolo


def test_the_first_error_is_kept(monkeypatch):
    monkeypatch.setattr(yolo, "_first_error", None)
    monkeypatch.setitem(sys.modules, "ultralytics", None)  # the import fails
    with pytest.raises(yolo.ModelLoadError, match="restart sortering-pnp") as first:
        yolo.YOLO("m")
    assert "import of ultralytics halted" in str(first.value)  # the real reason
    monkeypatch.delitem(sys.modules, "ultralytics")  # a later try would give torch's message
    with pytest.raises(yolo.ModelLoadError) as again:
        yolo.YOLO("m")
    assert str(again.value) == str(first.value)  # still the first, real reason
    assert isinstance(again.value, ValueError)  # shown on the page like a bad model
