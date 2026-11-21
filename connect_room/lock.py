"""The room lock: one mutating command at a time, with stale-lock detection and recovery."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

from . import proc
from .errors import Refused
from .store import now_iso

LOCK_NAME = "room.lock"
_FRESH_SECONDS = 5

StaleHandler = Callable[[dict | None], None]


class LockBusy(Refused):
    def __init__(self, holder: dict | None):
        self.holder = holder or {}
        super().__init__(
            f"room is busy: pid {self.holder.get('pid', '?')} is running "
            f"`{self.holder.get('command', '?')}` since {self.holder.get('started_at', '?')}")


def read_holder(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def holder_alive(path: Path) -> bool:
    holder = read_holder(path)
    if holder is None:
        try:
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age < _FRESH_SECONDS  # being written right now by another process
    return proc.is_alive(holder.get("pid"), holder.get("image"))


def acquire(room_dir: Path, command: str, on_stale: StaleHandler | None = None) -> None:
    path = room_dir / LOCK_NAME
    info = {"pid": os.getpid(), "command": command, "started_at": now_iso(), "image": sys.executable}
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if holder_alive(path):
                raise LockBusy(read_holder(path))
            if on_stale:
                on_stale(read_holder(path))
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        return
    raise LockBusy(read_holder(path))


def release(room_dir: Path) -> None:
    path = room_dir / LOCK_NAME
    holder = read_holder(path)
    if holder and holder.get("pid") == os.getpid():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


@contextlib.contextmanager
def locked(room_dir: Path, command: str, on_stale: StaleHandler | None = None) -> Iterator[None]:
    acquire(room_dir, command, on_stale)
    try:
        yield
    finally:
        release(room_dir)
