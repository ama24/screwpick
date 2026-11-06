"""Klipper through Moonraker, its web API (the same one Mainsail and Fluidd use; no OctoPrint).

G-code goes to POST /printer/gcode/script, which answers once Klipper has run it. The printer's
own menu reaches sortering_pnp through `action_call_remote_method("sortering_pnp", ...)` in a Klipper
macro: Moonraker forwards it to every websocket client that registered the method (MenuListener).
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from .base import PrinterError, PrinterStatus

log = logging.getLogger("sortering_pnp.moonraker")

REMOTE_METHOD = "sortering_pnp"


class Moonraker:
    def __init__(self, url: str = "http://localhost:7125", timeout_s: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode() or "{}").get("result")
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read().decode())["error"]["message"]
            except (ValueError, KeyError, TypeError):
                msg = str(exc)
            raise PrinterError(f"Klipper: {msg}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PrinterError(f"cannot reach Moonraker at {self.url}: {exc}") from exc

    def gcode(self, script: str) -> None:
        log.debug("gcode: %s", script.replace("\n", " | "))
        self._request("POST", "/printer/gcode/script", {"script": script})

    def info(self) -> dict[str, Any]:
        return self._request("GET", "/printer/info") or {}

    def status(self, tool_macro: str | None = None) -> PrinterStatus:
        info = self.info()
        st = PrinterStatus(
            state=str(info.get("state", "unknown")), message=str(info.get("state_message", ""))
        )
        if not st.ready:
            return st
        objs = {
            "toolhead": "position,homed_axes",
            "extruder": "temperature",
            "print_stats": "state",
        }
        if tool_macro:
            objs[f"gcode_macro {tool_macro}"] = "tool"
        q = "&".join(f"{urllib.parse.quote(k)}={v}" for k, v in objs.items())
        s = (self._request("GET", f"/printer/objects/query?{q}") or {}).get("status", {})
        th = s.get("toolhead", {})
        st.homed = str(th.get("homed_axes", ""))
        st.position = [float(v) for v in (th.get("position") or [0, 0, 0, 0])[:3]]
        t = s.get("extruder", {}).get("temperature")
        st.hotend_c = None if t is None else float(t)
        st.printing = s.get("print_stats", {}).get("state") in ("printing", "paused")
        if tool_macro:
            tool = s.get(f"gcode_macro {tool_macro}", {}).get("tool")
            st.tool = None if tool is None else str(tool).strip("\"'").lower()
        return st

    def emergency_stop(self) -> None:
        self._request("POST", "/printer/emergency_stop")

    def upload_config(self, name: str, text: str) -> None:
        """POST /server/files/upload into the "config" root (multipart form)."""
        boundary = "----sortering_pnp" + uuid.uuid4().hex
        crlf = "\r\n"
        head = crlf.join(
            [
                f"--{boundary}",
                'Content-Disposition: form-data; name="root"',
                "",
                "config",
                f"--{boundary}",
                f'Content-Disposition: form-data; name="file"; filename="{name}"',
                "Content-Type: text/plain",
                "",
                "",
            ]
        )
        body = (head + text + f"{crlf}--{boundary}--{crlf}").encode()
        req = urllib.request.Request(
            self.url + "/server/files/upload",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            raise PrinterError(f"Moonraker refused {name}: {exc}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PrinterError(f"cannot reach Moonraker at {self.url}: {exc}") from exc

    def firmware_restart(self) -> None:
        self._request("POST", "/printer/firmware_restart")

    def close(self) -> None:
        pass


def parse_remote_call(msg: dict[str, Any]) -> dict[str, Any] | None:
    """The arguments of a `sortering_pnp` remote-method call in a websocket message, else None."""
    if msg.get("method") != REMOTE_METHOD:
        return None
    params = msg.get("params") or {}
    if isinstance(params, list):  # older Moonraker versions wrap the kwargs in a list
        params = params[0] if params and isinstance(params[0], dict) else {}
    return {str(k).lower(): v for k, v in dict(params).items()}


class MenuListener:
    """Receives the printer-menu commands (Klipper macro SORTERING) and calls `on_command`.

    Runs in a background thread and reconnects when Klipper or Moonraker restarts."""

    def __init__(self, url: str, on_command: Callable[[dict[str, Any]], None]) -> None:
        self.ws_url = url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url += "/websocket"
        self.on_command = on_command
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="moonraker-menu", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import websocket  # websocket-client
        except ImportError:
            log.warning("printer-menu buttons off: pip install websocket-client")
            return
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self.ws_url, timeout=10)
                ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "server.connection.identify",
                            "params": {
                                "client_name": "sortering_pnp",
                                "version": "0.1.0",
                                "type": "other",
                                "url": "https://github.com/ama24/sortering-pnp",
                            },
                            "id": 1,
                        }
                    )
                )
                ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "connection.register_remote_method",
                            "params": {"method_name": REMOTE_METHOD},
                            "id": 2,
                        }
                    )
                )
                log.info("printer menu connected (%s)", self.ws_url)
                ws.settimeout(1.0)
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    call = parse_remote_call(json.loads(raw))
                    if call is not None:
                        log.info("printer menu: %s", call)
                        self.on_command(call)
                ws.close()
            except Exception as exc:  # noqa: BLE001 - keep listening whatever happened
                log.debug("printer menu connection: %s", exc)
            self._stop.wait(5.0)  # then reconnect
