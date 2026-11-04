"""A small HTTP server (standard library only) for the page in index.html and its JSON API.

GET  /                      the page
GET  /api/state             sorter state, latest picture, parts, events
POST /api/command           {"command": start|step|pause|resume|stop|estop, "strategy": ...}
GET  /api/layout            layout + problems;  POST /api/layout  save a layout
POST /api/scan              photo -> move bins to where their markers are
POST /api/calib/photo       photo -> markers;   POST /api/calib/record {"marker": id}
POST /api/calib/fit         fit + save;         POST /api/calib/clear
POST /api/calib/goto        {"marker": id};     POST /api/jog {"dx","dy","dz"};  GET /api/tip
GET  /api/print/bin?marker=100&fmt=3mf|stl, /api/print/baseplate, /api/print/labels,
     /api/print/all          printable files
GET  /runs/<file>           photos
GET  /api/models            stored models, active ones, what is loaded
POST /api/models/upload?filename=…&name=…   body = the file (zip, .onnx, .pt)
POST /api/models/activate   {"role": stage1|stage2|classifier, "id": … or null}
POST /api/models/weights    {"id", "path"};  /rename {"id", "name"};  /delete {"id"}
POST /api/models/test       {"id"}: run it on the last photo, nothing is switched
POST /api/bins/emptied      {"marker": id or null}: a bin was emptied (null: all ready ones)
GET  /api/session           who is logged in; POST /api/login, /api/setup (first account),
POST /api/logout;  GET /api/users, POST /api/users/add, /api/users/remove, /api/users/password
GET  /api/setup; POST /api/setup/printer|home|goto|machine|picker|confirm|reset   the Setup tab

With accounts (the default), everything except the page itself and the session routes needs the
session cookie set by /api/login; other requests get 401.
"""

from __future__ import annotations

import http.cookies
import http.server
import json
import logging
import threading
import urllib.parse
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from .. import binspec
from ..auth import COOKIE, Accounts, AuthError
from ..layout import Bin, Layout
from ..printables import export
from ..printer import PrinterError
from ..runner import Runner

log = logging.getLogger("screwpick.web")


def _page() -> bytes:
    return resources.files("screwpick.web").joinpath("index.html").read_bytes()


PUBLIC = {
    ("GET", "/"),
    ("GET", "/index.html"),
    ("GET", "/api/session"),
    ("POST", "/api/login"),
    ("POST", "/api/setup"),
    ("POST", "/api/logout"),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    runner: Runner  # set by make_server
    accounts: Accounts | None = None  # None: no login (web.require_login false)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base class signature
        log.debug("web: " + format, *args)

    # ------------------------------------------------------------------ helpers
    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str,
        filename: str | None = None,
        cookie: str | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200, cookie: str | None = None) -> None:
        self._send(code, json.dumps(data).encode(), "application/json", cookie=cookie)

    def _token(self) -> str | None:
        jar = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return jar[COOKIE].value if COOKIE in jar else None

    def _cookie(self, token: str, max_age: int) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"

    def _session(self, method: str, path: str) -> bool:
        """The login routes; True when the request was one of them (answered here)."""
        acc = self.accounts
        if path == "/api/session":
            user = acc.user_for(self._token()) if acc else None
            self._json(
                {
                    "login": acc is not None,
                    "user": user,
                    "first_account": bool(acc and not acc.has_users()),
                }
            )
            return True
        if acc is None:
            return False
        if path in ("/api/login", "/api/setup"):
            b = self._body()
            name, password = str(b.get("user", "")), str(b.get("password", ""))
            if path == "/api/setup":
                token = acc.first_user(name, password)
            else:
                token = acc.login(name, password, self.client_address[0])
            self.runner.note(f"web: {name.strip()} logged in from {self.client_address[0]}")
            self._json({"user": name.strip()}, cookie=self._cookie(token, acc.session_s))
            return True
        if path == "/api/logout":
            acc.logout(self._token())
            self._json({"user": None}, cookie=self._cookie("", 0))
            return True
        return False

    def _body(self) -> dict[str, Any]:
        data = json.loads(self._raw.decode() or "{}") if self._raw else {}
        return data if isinstance(data, dict) else {}

    def _route(self, method: str) -> None:
        url = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        # read every request's body before answering (uploads stream later): an unread body
        # makes the client see a reset connection
        self._raw = b""
        n = int(self.headers.get("Content-Length") or 0)
        if url.path != "/api/models/upload":
            if n > 1 << 20:
                self.close_connection = True
                self._json({"error": "request too large"}, 413)
                return
            self._raw = self.rfile.read(n) if n else b""
        try:
            if self._session(method, url.path):
                return
            acc = self.accounts
            if acc is not None and (method, url.path) not in PUBLIC:
                self.user = acc.user_for(self._token())
                if self.user is None:
                    if url.path == "/api/models/upload":
                        self.close_connection = True  # do not read a whole upload for nothing
                    self._json({"error": "please log in", "login": True}, 401)
                    return
            self._dispatch(method, url.path, q)
        except AuthError as exc:
            self._json({"error": str(exc)}, 403)
        except (PrinterError, ValueError, KeyError, TypeError, FileNotFoundError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001 - report it on the page
            log.exception("web request %s %s", method, url.path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self) -> None:  # noqa: N802 - base class name
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802 - base class name
        self._route("POST")

    # ------------------------------------------------------------------ routes
    def _dispatch(self, method: str, path: str, q: dict[str, str]) -> None:
        r = self.runner
        if method == "GET" and path in ("/", "/index.html"):
            self._send(200, _page(), "text/html; charset=utf-8")
        elif method == "GET" and path == "/api/state":
            self._json(r.snapshot())
        elif method == "POST" and path == "/api/command":
            b = self._body()
            self._json({"result": r.command(str(b.get("command", "")), b.get("strategy"))})
        elif method == "GET" and path == "/api/layout":
            lay = r.layout()
            self._json({"layout": lay.to_dict(), "problems": lay.problems(), "sizes": _sizes()})
        elif method == "POST" and path == "/api/layout":
            lay = Layout.from_dict(self._body().get("layout") or {})
            lay.save(r.layout_path)
            if lay.problems():
                r.setup.reset("layout")
            self._json({"layout": lay.to_dict(), "problems": lay.problems()})
        elif method == "GET" and path == "/api/setup":
            self._json(r.setup_state())
        elif method == "POST" and path == "/api/setup/printer":
            self._json(r.setup_check_printer())
        elif method == "POST" and path == "/api/setup/home":
            self._json(r.setup_home())
        elif method == "POST" and path == "/api/setup/goto":
            self._json({"tip": r.setup_goto(str(self._body()["target"])), **r.setup_state()})
        elif method == "POST" and path == "/api/setup/machine":
            self._json(r.save_machine(self._body().get("values") or {}))
        elif method == "POST" and path == "/api/setup/picker":
            self._json(r.setup_picker(str(self._body()["action"])))
        elif method == "POST" and path == "/api/setup/rotate":
            b = self._body()
            self._json(r.setup_rotate(b.get("angle"), bool(b.get("zero"))))
        elif method == "POST" and path == "/api/setup/confirm":
            self._json(r.setup_confirm(str(self._body()["step"])))
        elif method == "POST" and path == "/api/setup/reset":
            self._json(r.setup_reset(str(self._body()["step"])))
        elif method == "POST" and path == "/api/scan":
            self._json(r.scan_bins())
        elif method == "POST" and path == "/api/calib/photo":
            self._json({"markers": r.calib_photo(), "shot": r.state.shot})
        elif method == "POST" and path == "/api/calib/record":
            self._json(
                {"pair": r.calib_record(int(self._body()["marker"])), "pairs": r.calib_pairs}
            )
        elif method == "POST" and path == "/api/calib/fit":
            self._json(r.calib_fit())
        elif method == "POST" and path == "/api/calib/clear":
            r.calib_clear()
            self._json({"pairs": []})
        elif method == "POST" and path == "/api/calib/tip":
            self._json(r.tip_touches_plate())
        elif method == "POST" and path == "/api/calib/goto":
            self._json({"tip": r.goto_marker(int(self._body()["marker"]))})
        elif method == "POST" and path == "/api/jog":
            b = self._body()
            tip = r.jog(float(b.get("dx", 0)), float(b.get("dy", 0)), float(b.get("dz", 0)))
            self._json({"tip": tip})
        elif method == "GET" and path == "/api/tip":
            self._json({"tip": r.tip()})
        elif method == "GET" and path.startswith("/api/print/"):
            self._print(path.rsplit("/", 1)[-1], q)
        elif method == "GET" and path.startswith("/runs/"):
            self._file(r.runs_dir, path[len("/runs/") :])
        elif method == "GET" and path == "/api/models":
            self._json(r.models_state())
        elif method == "POST" and path == "/api/models/upload":
            self._upload(q)
        elif method == "POST" and path == "/api/models/activate":
            b = self._body()
            self._json(r.activate_model(str(b["role"]), b.get("id") or None))
        elif method == "POST" and path == "/api/models/weights":
            b = self._body()
            r._need_store().choose_weights(str(b["id"]), str(b["path"]))
            self._json(r.models_state())
        elif method == "POST" and path == "/api/models/rename":
            b = self._body()
            r._need_store().rename(str(b["id"]), str(b["name"]))
            self._json(r.models_state())
        elif method == "POST" and path == "/api/models/delete":
            r._need_store().delete(str(self._body()["id"]))
            self._json(r.models_state())
        elif method == "POST" and path == "/api/models/test":
            self._json(r.test_model(str(self._body()["id"])))
        elif method == "POST" and path == "/api/bins/emptied":
            m = self._body().get("marker")
            self._json(r.bin_emptied(None if m in (None, "") else int(m)))
        elif path.startswith("/api/users"):
            self._users(method, path)
        else:
            self._json({"error": f"not found: {method} {path}"}, 404)

    def _users(self, method: str, path: str) -> None:
        acc = self.accounts
        if acc is None:
            raise ValueError("logins are switched off (web.require_login: false)")
        if method == "POST":
            b = self._body()
            name = str(b.get("user", ""))
            if path == "/api/users/add":
                if name.strip() in acc.users():
                    raise AuthError(f"{name} exists already")
                acc.set_password(name, str(b.get("password", "")))
                self.runner.note(f"web: user {name.strip()} added by {self.user}")
            elif path == "/api/users/remove":
                acc.remove(name)
                self.runner.note(f"web: user {name} removed by {self.user}")
            elif path == "/api/users/password":
                if not acc.check(self.user or "", str(b.get("old", ""))):
                    raise AuthError("the current password is wrong")
                acc.set_password(self.user or "", str(b.get("password", "")))
                token = acc.login(self.user or "", str(b.get("password", "")))
                self._json(
                    {"users": acc.users(), "me": self.user},
                    cookie=self._cookie(token, acc.session_s),
                )
                return
            else:
                raise KeyError(path)
        self._json({"users": acc.users(), "me": self.user})

    def _upload(self, q: dict[str, str]) -> None:
        """The request body is the file itself (the page sends it with fetch, no form)."""
        r = self.runner
        store = r._need_store()
        filename = Path(q.get("filename") or "model.zip").name
        size = int(self.headers.get("Content-Length") or 0)
        limit = r.m.models.max_upload_mb * 1024 * 1024
        if size <= 0:
            raise ValueError("empty upload")
        if size > limit:
            self.close_connection = True
            raise ValueError(f"the file is larger than {r.m.models.max_upload_mb} MB")
        incoming = store.root / "_incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{uuid.uuid4().hex}.part"
        try:
            with tmp.open("wb") as f:
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ValueError("the upload was interrupted")
                    f.write(chunk)
                    left -= len(chunk)
            model = r.add_model(tmp, filename, q.get("name") or None)
        finally:
            tmp.unlink(missing_ok=True)
        self._json({"model": model, **r.models_state()})

    def _print(self, what: str, q: dict[str, str]) -> None:
        lay = self.runner.layout()
        if what == "bin":
            marker = int(q["marker"])
            b = lay.bin_by_marker(marker) or Bin(
                marker=marker,
                x=0,
                y=0,
                w=int(q.get("w", 1)),
                d=int(q.get("d", 1)),
                height_u=int(q.get("u", 3)),
            )
            name, data = export.bin_file(b, q.get("fmt", "3mf"))
            ctype = "model/3mf" if name.endswith(".3mf") else "model/stl"
        elif what == "baseplate":
            slim = q.get("kind", "slim") != "standard"
            name, data = export.baseplate_file(lay, float(q.get("lip", 8.0)), slim=slim)
            ctype = "model/stl"
        elif what == "plate":
            size = binspec.size_named(q["size"])
            cols, rows = export.plate_grid(size)
            first = int(q.get("first", size.markers.start))
            ids = [m for m in range(first, first + cols * rows) if m in size.markers]
            if not ids:
                raise ValueError(f"no {size.name} markers from {first}")
            name, data = export.plate_file(size, ids)
            ctype = "model/3mf"
        elif what == "labels":
            ids = [int(v) for v in q["markers"].split(",")] if q.get("markers") else None
            name, data = export.labels_file(lay, ids)
            ctype = "image/svg+xml"
        elif what == "all":
            name, data, ctype = "screwpick_print.zip", export.everything_zip(lay), "application/zip"
        else:
            raise ValueError(f"nothing to print called {what!r}")
        self._send(200, data, ctype, name)

    def _file(self, folder: Path, rel: str) -> None:
        name = urllib.parse.unquote(rel)
        target = (folder / name).resolve()
        if folder.resolve() not in target.parents or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = (
            "image/jpeg"
            if target.suffix.lower() in (".jpg", ".jpeg")
            else "application/octet-stream"
        )
        self._send(200, target.read_bytes(), ctype)


def _sizes() -> list[dict[str, Any]]:
    out = []
    for s in binspec.SIZES:
        cols, rows = export.plate_grid(s)
        out.append(
            {
                "name": s.name,
                "w": s.w,
                "d": s.d,
                "height_u": s.height_u,
                "first": s.markers.start,
                "last": s.markers.stop - 1,
                "per_plate": cols * rows,
                "use": s.use,
            }
        )
    return out


def make_server(
    runner: Runner, host: str = "", port: int = 8080, accounts: Accounts | None = None
) -> http.server.ThreadingHTTPServer:
    """`accounts` None: no login at all (only for a network you fully trust)."""
    handler = type("Handler", (_Handler,), {"runner": runner, "accounts": accounts, "user": None})
    last: OSError | None = None
    for p in range(port, port + 10):
        try:
            return http.server.ThreadingHTTPServer((host, p), handler)
        except OSError as exc:
            last = exc
    raise SystemExit(f"ports {port}-{port + 9} are all taken ({last})")


def serve_background(server: http.server.ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, name="web", daemon=True)
    t.start()
    return t
