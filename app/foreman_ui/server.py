"""Local web server for The Foreman UI: 127.0.0.1 only, a per-launch token on every request, no CORS.

Security model: docs/UI-DESIGN.md §3.3. All reads and writes go through room.py (commands.py).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from connect_room import __version__

from . import stacks
from .commands import DEFAULT_ROOMS_DIR, TOOL_HOME, Commands, InvalidInput, console_python, run, suggest_room_name, task_id

STATIC = Path(__file__).with_name("static")
PICK_FOLDER = Path(__file__).with_name("pick_folder.py")
STATIC_NAME = re.compile(r"^(fonts/)?[A-Za-z0-9][A-Za-z0-9._-]*\.(css|js|woff2)$")
TYPES = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
         "js": "text/javascript; charset=utf-8", "woff2": "font/woff2", "json": "application/json; charset=utf-8"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MAX_BODY = 64 * 1024
TOKEN_HEADER = "X-Foreman-Token"


def same_token(given: str | None, token: str) -> bool:
    if not given:
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8"))


def check_request(method: str, path: str, query_token: str | None, headers: dict[str, str | None],
                  token: str, port: int) -> str | None:
    """Why a request is refused, or None when it may proceed. Pure, so it is unit-tested."""
    if method not in ("GET", "POST"):
        return "method not allowed"
    if headers.get("host") != f"127.0.0.1:{port}":
        return "wrong host"
    origin = headers.get("origin")
    if origin is not None and origin != f"http://127.0.0.1:{port}":
        return "foreign origin"
    if headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        return "cross-site request"
    if path.startswith("/api/"):
        if not same_token(headers.get(TOKEN_HEADER.lower()), token):
            return "missing or wrong token"
        if method == "POST" and (headers.get("content-type") or "").split(";")[0].strip() != "application/json":
            return "POST needs application/json"
        return None
    if method != "GET":
        return "method not allowed"
    if not same_token(query_token, token):
        return "missing or wrong token"
    return None


RELEASE = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s+[—-]\s+(\d{4}-\d{2}-\d{2})\s*$")


def parse_changelog(text: str, limit: int = 10) -> list[dict]:
    """`## 1.1.0 — 2026-11-21` headings with `- item` bullets → [{version, date, items}], newest first."""
    releases: list[dict] = []
    for line in text.splitlines():
        m = RELEASE.match(line.strip())
        if m:
            releases.append({"version": m.group(1), "date": m.group(2), "items": []})
        elif releases and line.startswith("- "):
            releases[-1]["items"].append(line[2:].strip().replace("`", ""))
    return releases[:limit]


def copyright_line(license_text: str) -> str:
    m = re.search(r"^Copyright \(c\) (.+)$", license_text, re.M)
    return f"© {m.group(1).strip()}" if m else ""


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _git(repo: Path, *args: str) -> str | None:
    kwargs: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args], capture_output=True,
                           timeout=30, stdin=subprocess.DEVNULL, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, rooms_dir: Path | None = None, idle_minutes: float = 10):
        super().__init__(("127.0.0.1", 0), Handler)
        self.token = secrets.token_urlsafe(32)
        self.port = self.server_address[1]
        self.commands = Commands(rooms_dir) if rooms_dir else Commands()
        self.idle_s = idle_minutes * 60
        self.last_seen = time.monotonic()
        self._stopping = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def watch_idle(self) -> None:
        while not self._stopping.wait(15):
            if time.monotonic() - self.last_seen > self.idle_s:
                self.stop()

    def stop(self) -> None:
        if not self._stopping.is_set():
            self._stopping.set()
            threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server: UIServer
    server_version = "Foreman"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:   # noqa: A002 - never log URLs (they carry the token)
        return

    # ------------------------------------------------------------------ plumbing

    def _headers(self) -> dict[str, str | None]:
        return {k: self.headers.get(k) for k in ("host", "origin", "sec-fetch-site", "content-type",
                                                 TOKEN_HEADER.lower())}

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: object, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), TYPES["json"])

    def _refuse(self, status: int = 403) -> None:
        self._send(status, b"", "text/plain")

    def _guard(self) -> tuple[str, dict] | None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        reason = check_request(self.command, parts.path, (query.get("t") or [None])[0], self._headers(),
                               self.server.token, self.server.port)
        if reason:
            self._refuse(405 if reason == "method not allowed" else 403)
            return None
        self.server.touch()
        return parts.path, query

    def _body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY:
            raise InvalidInput("request too large")
        data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if not isinstance(data, dict):
            raise InvalidInput("request body must be a JSON object")
        return data

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:   # noqa: N802
        guarded = self._guard()
        if not guarded:
            return
        path, query = guarded
        if path == "/":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        self._api("GET", path, query, {})

    def do_POST(self) -> None:   # noqa: N802
        guarded = self._guard()
        if not guarded:
            return
        path, query = guarded
        try:
            body = self._body()
        except (InvalidInput, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        self._api("POST", path, query, body)

    def do_OPTIONS(self) -> None:   # noqa: N802 - no CORS, ever
        self._refuse(405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS

    def _static(self, name: str) -> None:
        if name != "index.html" and not STATIC_NAME.match(name):
            return self._refuse(404)
        path = STATIC / name
        if not path.is_file() or not inside(path, STATIC):
            return self._refuse(404)
        ext = name.rsplit(".", 1)[1]
        data = path.read_bytes()
        if ext in ("html", "css", "js"):
            data = data.replace(b"{{TOKEN}}", self.server.token.encode("ascii"))
        self._send(200, data, TYPES[ext])

    # ------------------------------------------------------------------ API

    def _api(self, method: str, path: str, query: dict, body: dict) -> None:
        route = ROUTES.get((method, path))
        if not route:
            return self._json({"error": "not found"}, 404)
        try:
            self._json(route(self, query, body))
        except InvalidInput as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:   # noqa: BLE001 - report, keep serving
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _room_json(self, argv: list[str], timeout: float = 120) -> dict:
        code, out, err = run(argv, timeout)
        try:
            data = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            data = None
        return {"code": code, "data": data, "output": out if data is None else "", "error": err.strip()}

    def api_info(self, query: dict, body: dict) -> dict:
        c = self.server.commands
        return {"tool_home": str(TOOL_HOME), "room_py": str(TOOL_HOME / "room.py"), "rooms_dir": str(c.rooms_dir),
                "python": sys.executable, "default_rooms_dir": c.rooms_dir == DEFAULT_ROOMS_DIR.resolve()}

    def api_about(self, query: dict, body: dict) -> dict:
        def read(name: str) -> str:
            try:
                return (TOOL_HOME / name).read_text(encoding="utf-8")
            except OSError:
                return ""
        license_text = read("LICENSE")
        changelog = parse_changelog(read("CHANGELOG.md"))
        version = __version__
        released = next((r["date"] for r in changelog if r["version"] == version), "")
        return {"version": version, "released": released, "copyright": copyright_line(license_text),
                "license": license_text.splitlines()[0].strip() if license_text else "",
                "changelog": changelog}

    def api_machine(self, query: dict, body: dict) -> dict:
        return self._room_json(self.server.commands.check())

    def api_rooms(self, query: dict, body: dict) -> dict:
        return self._room_json(self.server.commands.list())

    def api_show(self, query: dict, body: dict) -> dict:
        room, tid = (query.get("room") or [""])[0], (query.get("task") or [""])[0]
        return self._room_json(self.server.commands.show(room, tid))

    def api_doctor(self, query: dict, body: dict) -> dict:
        return self._room_json(self.server.commands.doctor(str(body.get("room", ""))))

    def api_detect(self, query: dict, body: dict) -> dict:
        folder = Path(str(body.get("path") or "").strip())
        if not folder.is_absolute() or not folder.is_dir():
            raise InvalidInput("choose an existing folder (a full path such as D:\\Code\\my-app)")
        top = _git(folder, "rev-parse", "--show-toplevel")
        if top is None:
            return {"is_repo": False, "path": str(folder.resolve())}
        repo = Path(top).resolve()
        dirty = _git(repo, "status", "--porcelain")
        name = suggest_room_name(str(repo))
        return {"is_repo": True, "path": str(repo), "picked_subfolder": repo != folder.resolve(),
                "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "main",
                "dirty": len(dirty.splitlines()) if dirty else 0, "name": name,
                "name_taken": (self.server.commands.room_dir(name) / "room.toml").exists(),
                **stacks.detect(repo)}

    def api_pick_folder(self, query: dict, body: dict) -> dict:
        code, out, err = run([console_python(), str(PICK_FOLDER)], timeout=600)
        path = out.strip()
        return {"path": path or None, "available": code != 3, "error": err.strip() if code not in (0, 3) else ""}

    def api_init(self, query: dict, body: dict) -> dict:
        c = self.server.commands
        argv = c.init(str(body.get("room", "")), str(body.get("repo", "")), str(body.get("base", "")),
                      body.get("verify"), body.get("protect"), body.get("deny"))
        code, out, err = run(argv)
        result = {"init": {"code": code, "output": out.strip(), "error": err.strip()}, "doctor": None}
        if code == 0:
            result["doctor"] = self._room_json(c.doctor(str(body["room"])))
        return result

    def _simple(self, argv: list[str]) -> dict:
        code, out, err = run(argv)
        return {"code": code, "output": out.strip(), "error": err.strip()}

    def api_pause(self, query: dict, body: dict) -> dict:
        return self._simple(self.server.commands.pause(str(body.get("room", "")), str(body.get("reason", ""))))

    def api_unpause(self, query: dict, body: dict) -> dict:
        return self._simple(self.server.commands.unpause(str(body.get("room", ""))))

    def api_stop(self, query: dict, body: dict) -> dict:
        return self._simple(self.server.commands.stop(str(body.get("room", "")), str(body.get("reason", ""))))

    def api_open_folder(self, query: dict, body: dict) -> dict:
        room_dir = self.server.commands.room_dir(str(body.get("room", "")))
        target = room_dir / "tasks" / task_id(str(body["task"])) if body.get("task") else room_dir
        if not target.is_dir() or not inside(target, room_dir):
            raise InvalidInput("folder not found")
        if not hasattr(os, "startfile"):
            raise InvalidInput("opening folders is supported on Windows only")
        os.startfile(str(target.resolve()))   # noqa: S606 - a folder inside the room, opened in Explorer
        return {"opened": str(target.resolve())}

    def api_heartbeat(self, query: dict, body: dict) -> dict:
        return {"ok": True}

    def api_quit(self, query: dict, body: dict) -> dict:
        self.server.stop()
        return {"ok": True}


ROUTES = {
    ("GET", "/api/info"): Handler.api_info,
    ("GET", "/api/about"): Handler.api_about,
    ("GET", "/api/machine"): Handler.api_machine,
    ("GET", "/api/rooms"): Handler.api_rooms,
    ("GET", "/api/show"): Handler.api_show,
    ("POST", "/api/doctor"): Handler.api_doctor,
    ("POST", "/api/detect"): Handler.api_detect,
    ("POST", "/api/pick-folder"): Handler.api_pick_folder,
    ("POST", "/api/init"): Handler.api_init,
    ("POST", "/api/pause"): Handler.api_pause,
    ("POST", "/api/unpause"): Handler.api_unpause,
    ("POST", "/api/stop"): Handler.api_stop,
    ("POST", "/api/open-folder"): Handler.api_open_folder,
    ("POST", "/api/heartbeat"): Handler.api_heartbeat,
    ("POST", "/api/quit"): Handler.api_quit,
}
