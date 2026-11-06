"""The one place that loads Ultralytics (and so torch, even for NCNN models).

torch can be imported only once per process: after a failed or half-done import (two web
requests importing it at the same moment, too little memory, a broken install) every later
try says only "`torch` can only be initialized once per process". So the import is done under
one lock, and the first real error is kept and repeated, with what to do about it."""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("sortering_pnp.yolo")

_lock = threading.RLock()
_first_error: str | None = None
RESTART = "restart sortering-pnp (sudo systemctl restart sortering-pnp) after fixing it"


class ModelLoadError(ValueError):
    """Ultralytics / torch cannot be loaded in this process (shown on the page)."""


def YOLO(path: str) -> Any:  # noqa: N802 - stands in for ultralytics.YOLO
    """ultralytics.YOLO(path), with the import guarded (see above)."""
    global _first_error
    with _lock:
        if _first_error is not None:
            raise ModelLoadError(
                f"the model library could not be loaded: {_first_error}; {RESTART}"
            )
        try:
            from ultralytics import YOLO as _YOLO
        except Exception as exc:
            _first_error = f"{type(exc).__name__}: {exc}"
            log.exception("loading ultralytics/torch failed")
            raise ModelLoadError(
                f"the model library could not be loaded: {_first_error}; {RESTART}"
            ) from exc
        return _YOLO(str(path))
