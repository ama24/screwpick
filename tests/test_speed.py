"""The page stays quick: the server answers on IPv6 and IPv4, and files the page asks about
every second are parsed again only when they change."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request

import pytest

from screwpick import files as files_mod
from screwpick.layout import Bin, Layout
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.runner import Runner
from screwpick.transform import Transform2D
from screwpick.web.server import make_server


def _runner(machine) -> Runner:
    Layout.default().save(machine.path(machine.paths.layout))
    world = SimWorld(seed=1)
    lay_path = machine.path(machine.paths.layout)
    vision = SimVision(world, lambda: Layout.load(lay_path), Transform2D.identity)
    return Runner(machine, SimPrinter(machine, world), vision)


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def test_the_server_answers_on_ipv4_and_ipv6(machine):
    server = make_server(_runner(machine), "", 18151, None)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        hosts = ["127.0.0.1"] + (["[::1]"] if _ipv6_loopback() else [])
        for host in hosts:
            with urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=10) as r:
                assert json.loads(r.read())["status"]
        assert server.request_queue_size >= 64
        second = make_server(_runner(machine), "", port, None)  # a second one moves on
        try:
            assert second.server_address[1] != port
        finally:
            second.server_close()
    finally:
        server.shutdown()
        server.server_close()


def test_the_layout_is_parsed_again_only_when_it_changes(machine):
    runner = _runner(machine)
    a = runner.layout()
    a.bins.append(Bin(marker=135, x=0, y=0))  # a copy: changing it changes nothing else
    assert runner.layout().bin_by_marker(135) is None
    assert runner.layout() is not runner.layout()
    first = runner._layout_cache  # noqa: SLF001
    runner.layout()
    assert runner._layout_cache is first  # not parsed again
    changed = runner.layout()
    changed.cols = 7
    time.sleep(0.01)
    changed.save(runner.layout_path)
    st = runner.layout_path.stat()  # make sure the time stamp moved on any file system
    os.utime(runner.layout_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert runner.layout().cols == 7


def test_file_summaries_follow_the_file(tmp_path):
    slot = files_mod.BY_KEY["classes"]
    p = tmp_path / "classes.yaml"
    p.write_text("names: {0: A, 1: B}\n", encoding="utf-8")
    assert files_mod._summary(slot, p) == "2 classes"  # noqa: SLF001
    p.write_text("names: {0: A, 1: B, 2: C}\n", encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert files_mod._summary(slot, p) == "3 classes"  # noqa: SLF001
    p.write_text("nope: 1\n", encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
    assert files_mod._summary(slot, p).startswith("cannot be used")  # noqa: SLF001


@pytest.mark.parametrize("text", ["a: 1\nb: [1, 2]\n", "x: {y: hello}\n"])
def test_fast_yaml_reads_the_same(tmp_path, text):
    import yaml

    from screwpick.io import read_yaml

    p = tmp_path / "f.yaml"
    p.write_text(text, encoding="utf-8")
    assert read_yaml(p) == yaml.safe_load(text)
