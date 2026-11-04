"""Wild bins (claimed by the first new kind) and the page login."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from screwpick.auth import Accounts, AuthError
from screwpick.layout import Bin, Layout
from screwpick.parts import Part
from screwpick.printer.sim import SimPrinter, SimVision, SimWorld
from screwpick.rules import destination, wild_key
from screwpick.runner import Runner
from screwpick.strategies import Planner
from screwpick.transform import Transform2D


def P(cls, length=None, has_length=None, unknown=False, xy=(110.0, 110.0)):  # noqa: N802
    hl = (length is not None) if has_length is None else has_length
    p = Part(1, cls, 0.9, xy, 0.0, np.zeros((0, 2)), 4.0, length, True, unknown, hl)
    p.bed_xy, p.gap_mm = xy, 10.0
    return p


def wild_layout(n: int = 3, wild_by: str = "class") -> Layout:
    lay = Layout(wild_by=wild_by)
    lay.bins = [Bin(marker=100 + i, x=i, y=0) for i in range(n)]
    return lay


# ------------------------------------------------------------------ rules
def test_wild_key():
    assert wild_key(P("DIN912_M4", 16.0), "part") == "DIN912_M4x16"
    assert wild_key(P("DIN912_M4", None, has_length=True), "part") is None  # needs the length
    assert wild_key(P("DIN934_M4"), "part") == "DIN934_M4"  # nuts have no length
    assert wild_key(P("DIN912_M4", 16.0), "class") == "DIN912_M4"
    assert wild_key(P("DIN912_M4", 16.0), "family") == "DIN912"
    assert wild_key(P("UNKNOWN", unknown=True), "family") == "UNKNOWN"


def test_only_wild_bins_claim_and_reuse():
    lay = wild_layout(2)
    claims: dict[int, str] = {}
    d = destination(P("DIN912_M4", 16.0), lay, 0.5, claims)
    assert d.bin.marker == 100 and d.claim == "DIN912_M4"
    claims[100] = "DIN912_M4"
    d = destination(P("DIN912_M4", 20.0), lay, 0.5, claims)
    assert d.bin.marker == 100 and d.claim is None  # same class, same bin
    d = destination(P("DIN934_M4"), lay, 0.5, claims)
    assert d.bin.marker == 101 and d.claim == "DIN934_M4"
    claims[101] = "DIN934_M4"
    d = destination(P("DIN125A_M4"), lay, 0.5, claims)  # all wild bins taken, no overflow
    assert d.bin is None and not d.waiting
    lay.bins.append(Bin(marker=110, x=3, y=0, role="overflow"))
    assert destination(P("DIN125A_M4"), lay, 0.5, claims).bin.marker == 110


def test_rules_come_before_wild_bins_and_unknown_parts_use_them():
    lay = wild_layout(2)
    lay.bins.append(Bin(marker=120, x=3, y=0, role="sort", holds=["DIN934*"]))
    assert destination(P("DIN934_M4"), lay, 0.5, {}).bin.marker == 120
    d = destination(P("UNKNOWN", unknown=True), lay, 0.5, {})
    assert d.bin.marker == 100 and d.claim == "UNKNOWN"


def test_wild_by_part_waits_for_the_length():
    lay = wild_layout(2, "part")
    d = destination(P("DIN912_M4", None, has_length=True), lay, 0.5, {})
    assert d.bin is None and d.waiting


def test_planner_commits_the_claim_only_after_a_good_drop(machine):
    pl = Planner(wild_layout(2), machine, "sort")
    part = P("DIN912_M3", 8.0)
    plan = pl.next([part])
    assert plan.claim == "DIN912_M3" and pl.claims == {}
    pl.report(plan, [part])  # still there: failed
    assert pl.claims == {}
    plan = pl.next([part])
    pl.report(plan, [])
    assert pl.claims == {100: "DIN912_M3"}


# ------------------------------------------------------------------ runs (simulation)
def make(machine, layout: Layout, n: int = 10, seed: int = 5, seen: Layout | None = None):
    lay_path = machine.path(machine.paths.layout)
    layout.save(lay_path)
    world = SimWorld(seed=seed)
    world.populate(layout, n=n)
    cam = seen or layout  # what the camera sees (bins and their markers)
    vision = SimVision(world, lambda: cam, Transform2D.identity)
    return Runner(machine, SimPrinter(machine, world), vision), world


def test_sorts_with_only_wild_bins_and_keeps_claims(machine):
    machine.run.shake.enabled = False
    lay = Layout.default()
    lay.bins = [b for b in lay.bins if b.role == "wild"]  # 13 wild bins, nothing else
    assert lay.wild_by == "part"  # the default: one part number (class + length) per bin
    runner, world = make(machine, lay, n=10)
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done", runner.state.message
    assert not world.parts
    claims = runner.claims
    assert claims and len(set(claims.values())) == len(claims)  # one kind per bin
    by_bin: dict[int, set[str]] = {}
    for sp, xy in world.dropped:
        mk = next(
            b.marker
            for b in lay.bins
            if lay.cells_rect(b.x, b.y, b.w, b.d)[0]
            <= xy[0]
            <= lay.cells_rect(b.x, b.y, b.w, b.d)[2]
            and lay.cells_rect(b.x, b.y, b.w, b.d)[1]
            <= xy[1]
            <= lay.cells_rect(b.x, b.y, b.w, b.d)[3]
        )
        by_bin.setdefault(mk, set()).add(sp.cls + (f"x{sp.length:g}" if sp.length else ""))
    assert all(len(v) == 1 for v in by_bin.values())
    assert {mk: next(iter(v)) for mk, v in by_bin.items()} == claims
    # the plate is empty: every filled bin is ready to be taken out
    assert {r["marker"] for r in runner.bins_state()["ready"]} == set(claims)
    assert sum(r["count"] for r in runner.bins_state()["ready"]) == len(world.dropped)
    # kept on disk, emptied from the page
    saved = json.loads(runner.claims_path.read_text())
    assert {int(k): v for k, v in saved["claims"].items()} == claims
    mk = next(iter(claims))
    state = runner.bin_emptied(mk)
    assert str(mk) not in state["claims"] and mk not in {r["marker"] for r in state["ready"]}
    assert runner.bin_emptied(None)["ready"] == []
    assert runner.claims == {} and runner.bin_counts == {}


def test_a_bin_is_ready_only_when_nothing_on_the_plate_goes_there(machine):
    lay = wild_layout(2, "part")
    lay.work.x, lay.work.y = 1, 1
    lay.cols = lay.rows = 5
    lay.bins = [Bin(marker=100, x=0, y=0), Bin(marker=101, x=1, y=0)]
    pl = Planner(lay, machine, "sort")
    pl.claims.update({100: "DIN912_M4x16", 101: "DIN934_M4"})
    plate = [P("DIN912_M4", 16.0)]
    assert pl.needed_bins(plate) == {100}
    assert pl.needed_bins([P("DIN912_M4", None, has_length=True)]) == {100}  # may be an M4x16
    assert pl.needed_bins([P("DIN912_M4", 16.0, xy=(10.0, 10.0))]) == set()  # not on the plate


def test_old_claims_file_still_loads(machine):
    path = machine.path(machine.paths.claims)
    path.write_text('{"105": "DIN912_M4"}')
    runner, _ = make(machine, Layout.default(), n=0)
    assert runner.claims == {105: "DIN912_M4"} and runner.bin_counts == {}


def test_bins_the_camera_finds_join_as_wild_and_unseen_ones_are_skipped(machine):
    machine.run.shake.enabled = False
    lay = Layout.default()
    lay.bins = [b for b in lay.bins if b.role == "wild"][:3]
    seen = Layout.from_dict(lay.to_dict())
    seen.bins = seen.bins[1:] + [Bin(marker=199, x=4, y=4)]  # 100.. is covered, 199 is new
    runner, world = make(machine, lay, n=4, seen=seen)
    runner.command("start", "sort")
    runner.wait(60)
    assert runner.state.status == "done", runner.state.message
    saved = runner.layout()
    assert saved.bin_by_marker(199) is not None and saved.bin_by_marker(199).role == "wild"
    used = {int(k) for k in runner.state.counts}
    first = lay.bins[0].marker
    assert first not in used and used <= {b.marker for b in seen.bins}
    assert any("not used" in e for e in runner.events)


# ------------------------------------------------------------------ accounts
def test_accounts(tmp_path):
    acc = Accounts(tmp_path / "users.json")
    assert not acc.has_users()
    token = acc.first_user("alex", "secret1")
    assert acc.user_for(token) == "alex" and acc.users() == ["alex"]
    with pytest.raises(AuthError, match="exists already"):
        acc.first_user("eve", "secret2")
    with pytest.raises(AuthError, match="6 characters"):
        acc.set_password("bob", "123")
    with pytest.raises(AuthError, match="wrong"):
        acc.login("alex", "nope", "1.2.3.4")
    t2 = acc.login("alex", "secret1", "1.2.3.4")
    assert acc.user_for(t2) == "alex"
    raw = (tmp_path / "users.json").read_text()
    assert "secret1" not in raw and token not in raw and t2 not in raw  # only hashes
    acc.logout(t2)
    assert acc.user_for(t2) is None
    acc.set_password("alex", "secret9")  # a new password ends the sessions
    assert acc.user_for(token) is None
    with pytest.raises(AuthError, match="last user"):
        acc.remove("alex")


def test_too_many_wrong_passwords(tmp_path, monkeypatch):
    monkeypatch.setattr("screwpick.auth.time.sleep", lambda s: None)
    acc = Accounts(tmp_path / "users.json")
    acc.set_password("alex", "secret1")
    for _ in range(10):
        with pytest.raises(AuthError, match="wrong"):
            acc.login("alex", "x", "9.9.9.9")
    with pytest.raises(AuthError, match="wait 5 minutes"):
        acc.login("alex", "secret1", "9.9.9.9")
    assert acc.login("alex", "secret1", "8.8.8.8")  # another address is fine


def test_web_login_flow(machine, tmp_path, monkeypatch):
    from screwpick.web.server import make_server

    monkeypatch.setattr("screwpick.auth.time.sleep", lambda s: None)
    runner, _ = make(machine, Layout.default(), n=1)
    acc = Accounts(tmp_path / "users.json")
    srv = make_server(runner, "127.0.0.1", 0, acc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, cookie=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data, {"Content-Type": "application/json"})
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), None

    try:
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            assert r.status == 200  # the page itself is public
        assert call("/api/session")[1] == {"login": True, "user": None, "first_account": True}
        assert call("/api/state")[0] == 401
        assert call("/api/command", {"command": "start"})[0] == 401
        assert runner.state.status == "idle"
        code, body, cookie = call("/api/setup", {"user": "alex", "password": "secret1"})
        assert code == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        jar = cookie.split(";")[0]
        assert call("/api/session", cookie=jar)[1]["user"] == "alex"
        assert call("/api/state", cookie=jar)[0] == 200
        assert call("/api/setup", {"user": "eve", "password": "secret2"})[0] == 403
        assert call("/api/login", {"user": "alex", "password": "bad"})[0] == 403
        code, body, _ = call("/api/users/add", {"user": "sam", "password": "secret3"}, jar)
        assert code == 200 and body["users"] == ["alex", "sam"]
        code, _, c2 = call("/api/login", {"user": "sam", "password": "secret3"})
        sam = c2.split(";")[0]
        assert call("/api/users/password", {"old": "wrong", "password": "newpass"}, sam)[0] == 403
        code, _, c3 = call("/api/users/password", {"old": "secret3", "password": "newpass"}, sam)
        assert code == 200 and call("/api/state", cookie=c3.split(";")[0])[0] == 200
        assert call("/api/state", cookie=sam)[0] == 401  # the old session ended
        assert call("/api/users/remove", {"user": "sam"}, jar)[1]["users"] == ["alex"]
        call("/api/logout", {}, jar)
        assert call("/api/state", cookie=jar)[0] == 401
    finally:
        srv.shutdown()
