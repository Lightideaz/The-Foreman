"""Turn UI actions into `room.py` argument lists. room.py stays the single source of truth.

Every builder validates its inputs and returns a list for subprocess (never a shell string). Free-text
values are passed as `--flag=value` or after `--`, so a value that starts with `-` is never read as a flag.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

TOOL_HOME = Path(__file__).resolve().parent.parent
ROOM_PY = TOOL_HOME / "room.py"
DEFAULT_ROOMS_DIR = TOOL_HOME / "rooms"

ALLOWED = frozenset({"check", "list", "status", "show", "doctor", "init", "pause", "unpause", "stop"})
ROOM_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TASK_ID = re.compile(r"^T-\d{4,}$")
BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<![./])$")
DENY_RULE = re.compile(r"^command\(regex:\^.+\$\)$")
MAX_TEXT = 300


def console_python(executable: str = sys.executable) -> str:
    """python.exe next to pythonw.exe. room.py must run as a console program: under pythonw (no console)
    every git or tasklist it starts would open, and flash, its own console window."""
    path = Path(executable)
    if path.name.lower() == "pythonw.exe":
        sibling = path.with_name("python.exe")
        if sibling.exists():
            return str(sibling)
    return executable


class InvalidInput(ValueError):
    """A UI value that may not reach room.py."""


def room_name(value: str) -> str:
    if not isinstance(value, str) or not ROOM_NAME.match(value):
        raise InvalidInput("room name: use 1-40 lower-case letters, digits and dashes")
    return value


def suggest_room_name(folder: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", Path(folder).name.lower()).strip("-")[:40].strip("-")
    return name or "project"


def task_id(value: str) -> str:
    if not isinstance(value, str) or not TASK_ID.match(value):
        raise InvalidInput("task id must look like T-0001")
    return value


def line(value: str, what: str, limit: int = MAX_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidInput(f"{what} must be text")
    value = value.strip()
    if (not value and not allow_empty) or len(value) > limit or any(c in value for c in "\r\n\x00"):
        raise InvalidInput(f"{what}: one line, 1-{limit} characters")
    return value


def text_list(values: object, what: str) -> list[str]:
    if not isinstance(values, list) or len(values) > 30:
        raise InvalidInput(f"{what} must be a list of at most 30 items")
    return [line(v, what) for v in values]


def deny_rules(values: object) -> list[str]:
    rules = text_list(values, "deny rule")
    for rule in rules:
        if not DENY_RULE.match(rule):
            raise InvalidInput(f"deny rule must look like command(regex:^...$): {rule}")
    return rules


def repo_paths(values: object) -> list[str]:
    """Protected paths are relative to the repository: no drive, no leading slash, no `..`."""
    paths = text_list(values, "protected path")
    for path in paths:
        parts = re.split(r"[\\/]", path)
        if re.match(r"^([A-Za-z]:|[\\/])", path) or ".." in parts:
            raise InvalidInput(f"protected path must be relative to the project, without '..': {path}")
    return paths


def branch(value: str) -> str:
    if not isinstance(value, str) or not BRANCH.match(value):
        raise InvalidInput("base branch: letters, digits, '.', '_', '-' and '/' only")
    return value


def repo_folder(value: str) -> Path:
    path = Path(line(value, "project folder", 1000))
    if not path.is_absolute() or not path.is_dir():
        raise InvalidInput("project folder must be an existing absolute folder path")
    return path.resolve()


class Commands:
    """argv builders bound to one rooms folder."""

    def __init__(self, rooms_dir: Path = DEFAULT_ROOMS_DIR, python: str | None = None):
        self.rooms_dir = Path(rooms_dir).resolve()
        self.python = python or console_python()

    def _base(self, command: str) -> list[str]:
        if command not in ALLOWED:
            raise InvalidInput(f"not a UI command: {command}")
        return [self.python, str(ROOM_PY)]

    def room_dir(self, name: str) -> Path:
        return self.rooms_dir / room_name(name)

    def _room(self, name: str, command: str) -> list[str]:
        return self._base(command) + ["--room", str(self.room_dir(name)), command]

    def check(self) -> list[str]:
        return self._base("check") + ["check", "--json"]

    def list(self) -> list[str]:
        return self._base("list") + ["list", "--json", f"--rooms-dir={self.rooms_dir}"]

    def status(self, name: str) -> list[str]:
        return self._room(name, "status") + ["--json"]

    def show(self, name: str, tid: str) -> list[str]:
        return self._room(name, "show") + [task_id(tid), "--json"]

    def doctor(self, name: str) -> list[str]:
        return self._room(name, "doctor") + ["--json"]

    def init(self, name: str, repo: str, base: str, verify: object = None, protect: object = None,
             deny: object = None) -> list[str]:
        argv = self._room(name, "init") + [f"--repo={repo_folder(repo)}", f"--base={branch(base)}"]
        argv += [f"--verify={v}" for v in text_list([] if verify is None else verify, "verify command")]
        argv += [f"--protect={p}" for p in repo_paths([] if protect is None else protect)]
        argv += [f"--deny={d}" for d in deny_rules([] if deny is None else deny)]
        return argv

    def pause(self, name: str, reason: str) -> list[str]:
        return self._room(name, "pause") + ["--", line(reason, "reason", allow_empty=True)]

    def unpause(self, name: str) -> list[str]:
        return self._room(name, "unpause")

    def stop(self, name: str, reason: str) -> list[str]:
        return self._room(name, "stop") + [f"--reason={line(reason, 'reason', allow_empty=True)}"]


def run(argv: list[str], timeout: float = 120) -> tuple[int, str, str]:
    """Run one room.py command; output is decoded as UTF-8 (room.py writes UTF-8)."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=str(TOOL_HOME), env=env, **kwargs)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
