"""Room and task state: file locations, lifecycle transitions and the one-open-task invariant."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, store
from .errors import NotFound, Refused, UsageError

OPEN = ("RUNNING", "AWAITING_REVIEW", "REWORK_READY")
TRANSITIONS = {
    "DRAFT": {"RUNNING"},
    "RUNNING": {"AWAITING_REVIEW"},
    "AWAITING_REVIEW": {"REWORK_READY", "ACCEPTED", "REJECTED"},
    "REWORK_READY": {"RUNNING", "REJECTED"},
}
TASK_ID = re.compile(r"^T-\d{4,}$")


def resolve_room_dir(arg: str) -> Path:
    """A bare name means rooms/<name> next to the app folder; anything path-like is used as a path."""
    if Path(arg).is_absolute() or "/" in arg or "\\" in arg:
        return Path(arg).resolve()
    return store.ROOMS_HOME / arg


def new_room_state() -> dict:
    return {"schema": 1, "next_task_number": 1, "open_task": None, "running": None}


@dataclass
class Room:
    dir: Path
    config: dict

    @classmethod
    def open(cls, room_dir: Path) -> "Room":
        if not (room_dir / "room.toml").exists():
            raise NotFound(f"no room at {room_dir} (run `init` first)")
        return cls(room_dir, config.load_room_config(room_dir / "room.toml"))

    @property
    def repo(self) -> Path:
        return Path(self.config["project"]["repo"])

    @property
    def paused(self) -> str | None:
        path = self.dir / "PAUSE"
        if not path.exists():
            return None
        try:
            return store.read_text(path).strip()
        except OSError:
            return ""

    def task_dir(self, tid: str) -> Path:
        return self.dir / "tasks" / tid

    def round_dir(self, tid: str, round_no: int) -> Path:
        return self.task_dir(tid) / "rounds" / str(round_no)

    def load_state(self) -> dict:
        return store.read_json(self.dir / "state.json", default=new_room_state())

    def save_state(self, st: dict) -> None:
        store.write_json(self.dir / "state.json", st)

    def load_task(self, tid: str) -> dict:
        if not TASK_ID.match(tid):
            raise UsageError(f"bad task id {tid!r} (expected something like T-0001)")
        task = store.read_json(self.task_dir(tid) / "task-state.json")
        if task is None:
            raise NotFound(f"task {tid} not found")
        return task

    def save_task(self, task: dict) -> None:
        store.write_json(self.task_dir(task["id"]) / "task-state.json", task)

    def task_ids(self) -> list[str]:
        root = self.dir / "tasks"
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if TASK_ID.match(p.name))

    def task_meta(self, tid: str) -> tuple[dict, str]:
        return config.parse_task(store.read_text(self.task_dir(tid) / "task.md"))

    def event(self, name: str, task: str | None = None, round_no: int | None = None, **data: Any) -> None:
        store.append_event(self.dir, name, task, round_no, **data)


def transition(task: dict, status: str, note: str = "") -> None:
    if status not in TRANSITIONS.get(task["status"], set()):
        raise Refused(f"{task['id']} is {task['status']}; it cannot move to {status}")
    task["status"] = status
    task.setdefault("history", []).append({"ts": store.now_iso(), "status": status, "note": note})
