"""Printer drivers: Moonraker (Klipper) and a simulator; Motion adds limits and the tool offset."""

from __future__ import annotations

from ..config import Machine
from .base import Printer, PrinterError, PrinterStatus
from .motion import Motion


def make_printer(m: Machine, world: object | None = None) -> Printer:
    if m.printer.driver == "sim":
        from .sim import SimPrinter, SimWorld

        return SimPrinter(m, world if isinstance(world, SimWorld) else SimWorld())
    from .moonraker import Moonraker

    return Moonraker(m.printer.url, m.printer.timeout_s)


__all__ = ["Motion", "Printer", "PrinterError", "PrinterStatus", "make_printer"]
