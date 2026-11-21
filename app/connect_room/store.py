"""Paths, atomic file writes, JSON helpers and the append-only event log."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

TOOL_HOME = Path(__file__).resolve().parent.parent          # <root>/app: the tool itself
ROOMS_HOME = TOOL_HOME.parent / "rooms"                     # <root>/rooms: local, never committed

# Indirection so tests can simulate the transient file locks that antivirus scanners cause on Windows.
_replace = os.replace


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str, retries: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(retries):
            try:
                _replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    return json.loads(text)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def append_event(room_dir: Path, event: str, task: str | None = None,
                 round_no: int | None = None, **data: Any) -> None:
    record = {"ts": now_iso(), "event": event, "task": task, "round": round_no, "data": data}
    with open(room_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
