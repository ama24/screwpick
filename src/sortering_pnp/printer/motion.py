"""Safe moves of the tool tip: soft limits, tool offset, travel height, and the pick/drop/shake
sequences built from them.

Positions are the TIP of the picker in printer coordinates; the toolhead is sent to
tip - tool.offset. Every move is checked against the configured limits before it is sent.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from ..config import Machine
from . import smooth
from .base import Printer, PrinterError, PrinterStatus

if TYPE_CHECKING:
    from ..pins import Pins

log = logging.getLogger("sortering_pnp.motion")


class Motion:
    def __init__(self, printer: Printer, machine: Machine, pins: Pins | None = None) -> None:
        from ..pins import make_pins  # pins.py needs this package: imported when used

        self.p = printer
        self.m = machine
        self.pins = pins if pins is not None else make_pins(machine, printer)  # the picker
        self.tip: list[float] | None = None  # last commanded tip position
        self.z_home_xy: tuple[float, float] | None = None  # tip X/Y for homing Z (set by runner)
        self._accel: float | None = None  # Klipper's acceleration, read once (0 = unknown)

    # ------------------------------------------------------------------ checks
    def preflight(self, home: bool = True, tool: bool | None = None) -> PrinterStatus:
        """Refuse to move unless the printer is idle, cold, homed and carries the picker.
        `home` False: never home here (setup moves): an unhomed printer is an error.
        `tool`: check that the picker is on (default: only for sorting, i.e. when `home`; the
        moves of the Setup tab come before the picker is put on, and someone watches them)."""
        pc = self.m.printer
        tool = home if tool is None else tool
        st = self.p.status(pc.tool_macro)
        if not st.ready:
            raise PrinterError(f"Klipper is not ready ({st.state}): {st.message}".strip(": "))
        if st.printing:
            raise PrinterError("the printer is printing")
        if st.hotend_c is not None and st.hotend_c > pc.max_hotend_c:
            raise PrinterError(f"the hotend is {st.hotend_c:.0f} C (limit {pc.max_hotend_c:.0f} C)")
        if pc.tool_macro and tool:
            if st.tool is None:
                raise PrinterError(
                    f"Klipper has no macro {pc.tool_macro}: add [include sortering_pnp.cfg] to printer.cfg"
                )
            if st.tool != "picker":
                raise PrinterError(
                    f"the active tool is {st.tool!r}: put the picker on, then Setup tab, Picker: "
                    f"OK (or {pc.tool_picker} in Klipper)"
                )
        if not all(a in st.homed for a in "xyz"):
            if not home or not pc.auto_home:
                raise PrinterError(
                    f"axes not homed ({st.homed or 'none'}): home first (Setup tab, Homing)"
                )
            self.home()
            st = self.p.status(pc.tool_macro)
        return st

    def home(self) -> None:
        """G28 through klipper/sortering_pnp.cfg's homing override: lift first, home X and Y, home Z
        with the tip over `z_home_xy` (the middle of the working area: flat plate, no bins)."""
        pc = self.m.printer
        lines = []
        if pc.home_macro and self.z_home_xy is not None:
            ox, oy, _ = self.m.tool.offset
            x, y = self.z_home_xy[0] - ox, self.z_home_xy[1] - oy  # toolhead over that spot
            self.check(self.z_home_xy[0], self.z_home_xy[1], pc.travel_z)
            lines += [
                f"SET_GCODE_VARIABLE MACRO={pc.home_macro} VARIABLE=z_home_x VALUE={x:.2f}",
                f"SET_GCODE_VARIABLE MACRO={pc.home_macro} VARIABLE=z_home_y VALUE={y:.2f}",
            ]
        log.info("homing")
        self.p.gcode("\n".join([*lines, "G28"]))
        self.tip = None

    def motors_off(self) -> None:
        """Quiet standby: the axes lose their homing and are homed again before the next move."""
        self.p.gcode("M84")
        self.tip = None

    def check(self, x: float, y: float, z: float) -> None:
        pc = self.m.printer
        ox, oy, oz = self.m.tool.offset
        for name, v, lim in (
            ("X", x - ox, pc.limits_x),
            ("Y", y - oy, pc.limits_y),
            ("Z", z - oz, pc.limits_z),
        ):
            if not (lim[0] - 1e-6 <= v <= lim[1] + 1e-6):
                raise PrinterError(f"move outside the limits: toolhead {name}{v:.2f} not in {lim}")

    # ------------------------------------------------------------------ moves
    def current_tip(self) -> list[float]:
        if self.tip is None:
            pos = self.p.status(None).position
            ox, oy, oz = self.m.tool.offset
            self.tip = [pos[0] + ox, pos[1] + oy, pos[2] + oz]
        return list(self.tip)

    def move(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        feed: float | None = None,
    ) -> None:
        cur = self.current_tip()
        tx = cur[0] if x is None else x
        ty = cur[1] if y is None else y
        tz = cur[2] if z is None else z
        self.check(tx, ty, tz)
        ox, oy, oz = self.m.tool.offset
        words = []
        if x is not None:
            words.append(f"X{tx - ox:.3f}")
        if y is not None:
            words.append(f"Y{ty - oy:.3f}")
        if z is not None:
            words.append(f"Z{tz - oz:.3f}")
        if not words:
            return
        f = feed or (self.m.printer.feed_z if x is None and y is None else self.m.printer.feed_xy)
        last = f"G1 {' '.join(words)}"
        if self.m.printer.y_motion.enabled and abs(ty - cur[1]) > 1e-6:
            self.p.gcode("\n".join(["G90", *self._gentle_y(cur, [tx, ty, tz], f, last)]))
        else:
            self.p.gcode(f"G90\n{last} F{f:.0f}")
        self.tip = [tx, ty, tz]

    def _normal_accel(self) -> float | None:
        """The acceleration X and Z keep: printer.accel, else Klipper's (read once: Motion
        always sets it back after a bed move). None: unknown."""
        if self.m.printer.accel:
            return float(self.m.printer.accel)
        if self._accel is None:
            self._accel = self.p.status(None).max_accel
            if self._accel is None:
                log.warning("the printer's acceleration is unknown: bed moves leave it lowered")
                self._accel = 0.0
        return self._accel or None

    def _gentle_y(self, cur: list[float], to: list[float], feed: float, last: str) -> list[str]:
        """A move that moves the bed (printer.y_motion): lowered acceleration while it runs,
        with `s_curve` split into straight G1 steps whose speeds follow an S; `last` (the
        target, as a plain move would send it) ends it, so the end point is exact."""
        ym = self.m.printer.y_motion
        delta = [b - a for a, b in zip(cur, to, strict=True)]
        length = math.sqrt(sum(d * d for d in delta))
        f, a = smooth.path_limits(length, delta[1], feed, ym.accel, ym.max_feed)
        normal = self._normal_accel()
        if normal is not None:
            a = min(a, normal)
        steps = (
            smooth.s_curve(length, f, a, ym.steps, ym.min_step_mm)
            if ym.profile == "s_curve"
            else [(length, f)]
        )
        lines = []
        if normal is None or a < normal - 1e-6:
            lines.append(f"SET_VELOCITY_LIMIT ACCEL={a:.0f}")
        ox, oy, oz = self.m.tool.offset
        axes = ((0, "X", ox), (1, "Y", oy), (2, "Z", oz))
        for dist, sf in steps[:-1]:
            t = dist / length
            p = [c + t * d for c, d in zip(cur, delta, strict=True)]
            self.check(*p)  # on the straight line between two checked points: inside
            words = [f"{n}{p[i] - o:.3f}" for i, n, o in axes if abs(delta[i]) > 1e-9]
            lines.append(f"G1 {' '.join(words)} F{max(sf, 1.0):.0f}")
        lines.append(f"{last} F{max(steps[-1][1], 1.0):.0f}")
        if normal is not None and a < normal - 1e-6:
            lines.append(f"SET_VELOCITY_LIMIT ACCEL={normal:.0f}")
        return lines

    def travel(self, x: float, y: float) -> None:
        """Up to the travel height first, then across."""
        tz = self.m.printer.travel_z
        if self.current_tip()[2] < tz - 1e-6:
            self.move(z=tz)
        self.move(x=x, y=y)

    def photo_position(self) -> None:
        x, y, z = self.m.printer.photo_position
        self.travel(x, y)
        self.move(z=z)
        self.p.gcode("M400")

    def change_position(self) -> None:
        """Where the head waits while the picker is put on (printer.change_position)."""
        x, y, z = self.m.printer.change_position
        self.travel(x, y)
        self.move(z=z)
        self.p.gcode("M400")

    def jog(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0) -> list[float]:
        x, y, z = self.current_tip()
        self.move(x + dx, y + dy, z + dz, feed=self.m.printer.feed_z if dz else 1500.0)
        self.p.gcode("M400")
        return self.current_tip()

    # ------------------------------------------------------------------ sequences
    def turn(self, angle_deg: float) -> None:
        """Tweezers: the jaws to `angle_deg` (0..180, offset added): the servo on the Pi's turn
        pin, or a Klipper macro when they turn on a stepper (picker.rotate_macro)."""
        a = angle_deg % 180.0
        macro = self.m.picker.rotate_macro
        if macro:
            self.p.gcode(f"{macro} ANGLE={a:.1f}")
        else:
            self.pins.turn(a)

    def pick(self, x: float, y: float, z: float, angle_deg: float | None = None) -> None:
        """Go to the part, descend slowly to z (tip height), grab, lift to the travel height."""
        pk = self.m.picker
        self.travel(x, y)
        if angle_deg is not None and pk.kind.rotates:
            # the jaws close across the part: `angle_deg` = their closing direction (grip.py)
            self.turn(angle_deg)
        self.move(z=z + pk.approach_mm)
        self.move(z=z, feed=pk.feed_z_slow)
        self.p.gcode("M400")  # the pins are not in Klipper's queue: wait for the moves first
        self.pins.grab()
        self.move(z=z + pk.approach_mm, feed=pk.feed_z_slow)
        self.move(z=self.m.printer.travel_z)

    def drop(self, x: float, y: float, rim_z: float) -> None:
        """Over the bin, let go: at the travel height, or lowered to rim + drop_mm when
        picker.lower_to_drop is on. Never below rim + drop_mm, also when travel_z is low."""
        pk = self.m.picker
        self.travel(x, y)
        tz = self.m.printer.travel_z
        drop_z = rim_z + pk.drop_mm
        if not pk.lower_to_drop:
            drop_z = max(drop_z, tz)
        if abs(drop_z - self.current_tip()[2]) > 1e-6:
            self.move(z=drop_z)
        self.p.gcode("M400")
        self.pins.release()
        self.move(z=self.m.printer.travel_z)

    def shake(self) -> None:
        """Quick back-and-forth moves of the bed (or head) to spread a clump of parts. They are
        meant to be sharp: sent as they are, without printer.y_motion's gentle ramps."""
        sh = self.m.run.shake
        axis = sh.axis.upper()
        self.photo_position()
        lines = ["M400", "G91"]
        for i in range(sh.moves):
            sign = 1 if i % 2 == 0 else -1
            lines.append(f"G1 {axis}{sign * sh.distance_mm:.2f} F{sh.feed:.0f}")
        if sh.moves % 2:
            lines.append(f"G1 {axis}{-sh.distance_mm:.2f} F{sh.feed:.0f}")
        lines += ["G90", "M400"]
        # the photo position is inside the limits; shake moves stay within +-distance of it
        x, y, z = self.current_tip()
        lo = [v - sh.distance_mm for v in (x, y)]
        hi = [v + sh.distance_mm for v in (x, y)]
        for cx, cy in ((lo[0], lo[1]), (hi[0], hi[1])):
            self.check(cx if axis == "X" else x, cy if axis == "Y" else y, z)
        self.p.gcode("\n".join(lines))

    def message(self, text: str) -> None:
        """A line on the printer's display."""
        try:
            self.p.gcode("M117 " + text.replace("\n", " ")[:40])
        except PrinterError as exc:
            log.debug("M117: %s", exc)
