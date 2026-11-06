"""What sortering_pnp needs from a printer: run G-code, report its state, stop at once."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class PrinterError(RuntimeError):
    pass


@dataclass
class PrinterStatus:
    state: str = "unknown"  # Klipper: ready | startup | shutdown | error
    message: str = ""
    homed: str = ""  # homed axes, e.g. "xyz"
    position: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # toolhead X Y Z
    hotend_c: float | None = None
    printing: bool = False
    tool: str | None = None  # the _SORTERING_TOOL macro's `tool` variable, None = not found

    @property
    def ready(self) -> bool:
        return self.state == "ready"


class Printer(Protocol):
    def gcode(self, script: str) -> None:
        """Run G-code (several lines allowed) and return when the printer accepted it all."""

    def status(self, tool_macro: str | None = None) -> PrinterStatus: ...

    def emergency_stop(self) -> None: ...

    def close(self) -> None: ...
