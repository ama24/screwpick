"""sortering-pnp command line.

sortering-pnp serve              web page + sorter + printer-menu buttons (the normal way to run it)
sortering-pnp serve --sim        the same with a simulated printer, parts and camera (try it on a PC)
sortering-pnp run --strategy sort [--steps N]   sort without the web page (logs to the terminal)
sortering-pnp check              is everything there? (config, layout, calibration, printer)
sortering-pnp user add NAME      page account (new user or new password); user remove / list
sortering-pnp print bin|baseplate|labels|all    write printable files
sortering-pnp print catalogue --out prints      every bin size as full build plates, baseplates, labels
sortering-pnp print mounts --reach 135 --bar-height 364   screen holder + camera arm for the top bar
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import config as config_mod
from .config import Machine

log = logging.getLogger("sortering_pnp")

DEFAULT_CONFIG = Path("configs/machine.yaml")


def _load(args: argparse.Namespace) -> Machine:
    path = args.config
    if path is None and DEFAULT_CONFIG.is_file():
        path = DEFAULT_CONFIG
    m = config_mod.load(path)
    if getattr(args, "sim", False):
        m.printer.driver = "sim"
    return m


def _build(m: Machine):  # noqa: ANN202 - (runner, world)
    from .layout import Layout
    from .models import make_store
    from .printer import make_printer
    from .runner import Runner

    runs = m.path(m.paths.runs)
    assert runs is not None
    store = make_store(m)
    if m.printer.driver == "sim":
        from .printer.sim import SimVision, SimWorld
        from .transform import BedCalibration, Transform2D

        layout_path = m.path(m.paths.layout)
        transform_path = m.path(m.paths.transform)
        assert layout_path is not None and transform_path is not None
        world = SimWorld()
        world.populate(Layout.load(layout_path), n=14)

        def transform() -> Transform2D:
            cal = BedCalibration.load(transform_path)
            return cal.transform if cal else Transform2D.identity()

        vision = SimVision(world, lambda: Layout.load(layout_path), transform, runs)
        printer = make_printer(m, world)
        if m.camera.source == "head":
            vision.head_tip = printer._tip  # noqa: SLF001 - the simulated tip
            vision.head = m.camera.head
            vision.head_ref = tuple(m.printer.photo_position[:2])
        return Runner(m, printer, vision, store), world
    from .vision import CameraVision

    return Runner(m, make_printer(m), CameraVision(m, runs, store), store), None


def cmd_serve(args: argparse.Namespace) -> int:
    from .printer.moonraker import MenuListener
    from .web.server import make_server

    m = _load(args)
    runner, world = _build(m)
    server = make_server(runner, m.web.host, args.port or m.web.port, _accounts(m))
    listener = None
    if m.printer.driver == "moonraker":

        def on_menu(call: dict) -> None:
            print(
                runner.command(str(call.get("command", "")), call.get("strategy") or None),
                flush=True,
            )

        listener = MenuListener(m.printer.url, on_menu)
        listener.start()
    auto, strategy = runner.saved_mode()
    if auto:
        print(f"automatic mode was on: {runner.command('auto', strategy)}", flush=True)
    port = server.server_address[1]
    print(
        f"sortering-pnp: open http://{_address()}:{port}/ in a browser (Ctrl+C to quit)", flush=True
    )
    if world is not None:
        print("simulated printer, parts and camera: nothing real moves", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        runner.shutdown()  # automatic mode stays remembered for the next start
        if listener is not None:
            listener.stop()
        server.server_close()
    return 0


def _accounts(m: Machine):  # noqa: ANN202 - Accounts | None
    from .auth import Accounts

    if not m.web.require_login:
        log.warning("web.require_login is false: anyone on the network can use the page")
        return None
    path = m.path(m.paths.users)
    assert path is not None
    return Accounts(path, m.web.session_days)


def cmd_user(args: argparse.Namespace) -> int:
    """Manage page accounts from the Pi's terminal (e.g. a forgotten password)."""
    import getpass

    from .auth import Accounts, AuthError

    m = _load(args)
    path = m.path(m.paths.users)
    assert path is not None
    acc = Accounts(path, m.web.session_days)
    try:
        if args.action == "list":
            print("\n".join(acc.users()) or "no users yet: the page asks for the first one")
        elif args.action == "remove":
            acc.remove(args.name)
            print(f"removed {args.name}")
        else:
            if not args.name:
                raise AuthError("give a user name: sortering-pnp user add NAME")
            pw = getpass.getpass(f"password for {args.name}: ")
            if pw != getpass.getpass("again: "):
                raise AuthError("the passwords differ")
            acc.set_password(args.name, pw)
            print(f"{args.name} can log in now")
    except AuthError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    m = _load(args)
    runner, world = _build(m)
    print(runner.command("step" if args.steps == 1 else "start", args.strategy))
    seen = 0
    try:
        while runner._thread is not None and runner._thread.is_alive():
            events = list(runner.events)
            for e in events[seen:]:
                print(e, flush=True)
            seen = len(events)
            if runner.state.status == "stuck" and not sys.stdin.isatty():
                runner.command("stop")
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("stopping after this move...")
        runner.command("stop")
        runner.wait()
    for e in list(runner.events)[seen:]:
        print(e)
    s = runner.state
    print(f"{s.status}: {s.message}; {s.picks} part(s) sorted, {s.failed_picks} failed pick(s)")
    return 0 if s.status in ("done", "idle") else 1


def cmd_check(args: argparse.Namespace) -> int:
    from .layout import Layout
    from .printer import PrinterError, make_printer
    from .printer.motion import Motion
    from .transform import BedCalibration

    m = _load(args)
    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        print(("  ok   " if good else "  !!   ") + text)

    lay_path = m.path(m.paths.layout)
    assert lay_path is not None
    lay = Layout.load(lay_path)
    problems = lay.problems()
    line(
        lay_path.is_file(),
        f"layout {lay_path}"
        + ("" if lay_path.is_file() else " (missing: the default 5x5 layout is used)"),
    )
    for p in problems:
        line(False, f"layout: {p}")
    tpath = m.path(m.paths.transform)
    assert tpath is not None
    cal = BedCalibration.load(tpath)
    line(
        cal is not None or m.printer.driver == "sim",
        f"camera-to-printer calibration {tpath}" + ("" if cal else " (missing: Calibrate tab)"),
    )
    if cal is not None and cal.residuals_mm:
        worst = max(cal.residuals_mm)
        line(worst < 1.0, f"calibration error up to {worst:.2f} mm")
    try:
        printer = make_printer(m)
        st = Motion(printer, m).p.status(m.printer.tool_macro)
        line(st.ready, f"printer: Klipper {st.state} {st.message}".rstrip())
        if st.ready:
            line(st.tool == "picker" or not m.printer.tool_macro, f"active tool: {st.tool}")
            line(
                st.hotend_c is None or st.hotend_c <= m.printer.max_hotend_c,
                f"hotend {st.hotend_c} C",
            )
            line(True, f"homed axes: {st.homed or 'none'} (homed automatically on start)")
    except PrinterError as exc:
        line(False, f"printer: {exc}")
    print("all good" if ok else "fix the lines marked !!")
    return 0 if ok else 1


def cmd_print(args: argparse.Namespace) -> int:
    from .layout import Bin, Layout
    from .printables import export

    m = _load(args)
    lay_path = m.path(m.paths.layout)
    assert lay_path is not None
    lay = Layout.load(lay_path)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, bytes]] = []
    if args.what == "bin":
        markers = args.marker or [b.marker for b in lay.bins]
        for mk in markers:
            b = lay.bin_by_marker(mk) or Bin(
                marker=mk, x=0, y=0, w=args.w, d=args.d, height_u=args.height
            )
            files.append(export.bin_file(b, args.fmt))
    elif args.what == "baseplate" and args.bed:
        files += export.bed_baseplate_files(args.bed, not args.standard, lay.cols, lay.rows)
    elif args.what == "baseplate":
        files.append(export.baseplate_file(lay, args.lip, slim=not args.standard))
    elif args.what == "mounts":  # screen holder and camera arm for the printer's top bar
        from .printables.mounts import CameraArm, ScreenHolder, drop_for, mount_files

        arm = CameraArm(reach=args.reach)
        if args.bar_height is not None:
            arm.drop = drop_for(args.bar_height, args.lens_height, arm)
        files += mount_files(ScreenHolder(tilt_deg=args.tilt), arm)
    elif args.what == "catalogue":  # the ready-made set (prints/ in the repository)
        export.catalogue(out)
        return 0
    elif args.what == "labels":
        files.append(export.labels_file(lay, args.marker or None))
    else:
        files.append(("sortering_pnp_print.zip", export.everything_zip(lay)))
    for name, data in files:
        (out / name).write_bytes(data)
        print(out / name)
    return 0


def _address() -> str:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname() + ".local"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sortering-pnp",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--config", type=Path, default=None, help="machine.yaml (default: configs/machine.yaml)"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="web page + sorter + printer menu")
    s.add_argument("--sim", action="store_true", help="simulated printer, parts and camera")
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(func=cmd_serve)
    r = sub.add_parser("run", help="sort without the web page")
    r.add_argument("--strategy", default=None, help="sort | clear | kit")
    r.add_argument(
        "--steps", type=int, default=None, help="stop after this many picks (1 = one pick)"
    )
    r.add_argument("--sim", action="store_true")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("check", help="check config, layout, calibration and printer")
    c.add_argument("--sim", action="store_true")
    c.set_defaults(func=cmd_check)
    u = sub.add_parser("user", help="page accounts: add (or new password), remove, list")
    u.add_argument("action", choices=["add", "remove", "list"])
    u.add_argument("name", nargs="?", default="")
    u.set_defaults(func=cmd_user)
    p = sub.add_parser("print", help="write printable bins, baseplate or labels")
    p.add_argument("what", choices=["bin", "baseplate", "labels", "all", "catalogue", "mounts"])
    p.add_argument(
        "--reach", type=float, default=135.0, help="mounts: bar front face to plate centre, bed out"
    )
    p.add_argument(
        "--bar-height", type=float, default=None, help="mounts: bar underside above the plate"
    )
    p.add_argument("--lens-height", type=float, default=300.0, help="mounts: lens above the plate")
    p.add_argument("--tilt", type=float, default=20.0, help="mounts: screen leans back (deg)")
    p.add_argument(
        "--marker", type=int, action="append", help="bin marker id (repeat; default: every bin)"
    )
    p.add_argument("--fmt", choices=["3mf", "stl"], default="3mf")
    p.add_argument("--w", type=int, default=1)
    p.add_argument("--d", type=int, default=1)
    p.add_argument("--height", type=int, default=3, help="height units (7 mm), at least 3")
    p.add_argument(
        "--lip", type=float, default=8.0, help="baseplate: lip height around the working area"
    )
    p.add_argument(
        "--bed", choices=["ender3", "mk3"], help="baseplate clamped to that whole printer bed"
    )
    p.add_argument(
        "--standard", action="store_true", help="baseplate: standard 4.65 mm (default: slim 2.5 mm)"
    )
    p.add_argument("--out", default="print")
    p.set_defaults(func=cmd_print)
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
