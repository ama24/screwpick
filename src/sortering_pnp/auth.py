"""Accounts for the web page: user names with salted, slow password hashes, and login sessions.

users.json holds {"users": {name: {"salt", "hash", "iterations", "created"}}, "sessions": {...}}.
Sessions are random tokens in a cookie; only their SHA-256 is stored, so the file never holds
anything that logs in by itself. While there is no account, the page asks the first visitor to
create one.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .io import read_json, write_json

ITERATIONS = 200_000
MIN_PASSWORD = 6
COOKIE = "sortering_pnp_session"


class AuthError(ValueError):
    pass


def _hash(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, path: Path, session_days: int = 30) -> None:
        self.path = path
        self.session_s = session_days * 86400
        self._lock = threading.RLock()
        self._fails: dict[str, list[float]] = {}  # client address -> recent failed logins

    # ------------------------------------------------------------------ file
    def _read(self) -> dict[str, Any]:
        d = read_json(self.path) if self.path.is_file() else {}
        d.setdefault("users", {})
        d.setdefault("sessions", {})
        return d

    def _write(self, d: dict[str, Any]) -> None:
        write_json(self.path, d)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)  # only the Pi's user may read it

    # ------------------------------------------------------------------ users
    def users(self) -> list[str]:
        return sorted(self._read()["users"])

    def has_users(self) -> bool:
        return bool(self._read()["users"])

    def set_password(self, name: str, password: str) -> None:
        """Create a user or change its password."""
        name = name.strip()
        if not name or len(name) > 40 or any(c.isspace() for c in name):
            raise AuthError("user name: 1-40 characters, no spaces")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"the password needs at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        with self._lock:
            d = self._read()
            created = d["users"].get(name, {}).get("created", time.strftime("%Y-%m-%d %H:%M:%S"))
            d["users"][name] = {
                "salt": salt.hex(),
                "hash": _hash(password, salt, ITERATIONS),
                "iterations": ITERATIONS,
                "created": created,
            }
            # a new password ends this user's other sessions
            d["sessions"] = {k: v for k, v in d["sessions"].items() if v["user"] != name}
            self._write(d)

    def remove(self, name: str) -> None:
        with self._lock:
            d = self._read()
            if name not in d["users"]:
                raise AuthError(f"no user {name!r}")
            if len(d["users"]) == 1:
                raise AuthError("the last user cannot be removed (it would open the page to all)")
            del d["users"][name]
            d["sessions"] = {k: v for k, v in d["sessions"].items() if v["user"] != name}
            self._write(d)

    def check(self, name: str, password: str) -> bool:
        u = self._read()["users"].get(name.strip())
        if u is None:
            _hash(password, b"0" * 16, ITERATIONS)  # same time whether the user exists or not
            return False
        got = _hash(password, bytes.fromhex(u["salt"]), int(u["iterations"]))
        return hmac.compare_digest(got, u["hash"])

    # ------------------------------------------------------------------ sessions
    def login(self, name: str, password: str, client: str = "") -> str:
        """A new session token, or AuthError. Too many failures from one address: wait."""
        now = time.time()
        recent = [t for t in self._fails.get(client, []) if now - t < 300]
        if len(recent) >= 10:
            raise AuthError("too many wrong passwords: wait 5 minutes")
        if not self.check(name, password):
            self._fails[client] = [*recent, now]
            time.sleep(0.5)
            raise AuthError("wrong user name or password")
        self._fails.pop(client, None)
        return self._new_session(name.strip())

    def _new_session(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            d = self._read()
            now = time.time()
            d["sessions"] = {k: v for k, v in d["sessions"].items() if v["expires"] > now}
            d["sessions"][_token_id(token)] = {"user": name, "expires": now + self.session_s}
            self._write(d)
        return token

    def first_user(self, name: str, password: str) -> str:
        """Create the first account (only while there is none) and log it in."""
        with self._lock:
            if self.has_users():
                raise AuthError("an account exists already: log in")
            self.set_password(name, password)
            return self._new_session(name.strip())

    def user_for(self, token: str | None) -> str | None:
        if not token:
            return None
        s = self._read()["sessions"].get(_token_id(token))
        if s is None or s["expires"] < time.time():
            return None
        return str(s["user"])

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            d = self._read()
            if d["sessions"].pop(_token_id(token), None) is not None:
                self._write(d)
