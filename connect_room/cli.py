"""Command-line interface: argument parsing, exit codes, and the read-only commands."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
import urllib.parse
from pathlib import Path

from . import __version__, agent, config, dispatch, gitops, lifecycle, lock, proc, render, store
from .errors import RoomError, UsageError, Refused
from .state import Room, new_room_state, resolve_room_dir

TEMPLATES = store.TOOL_HOME / "templates"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="room.py", description="The Foreman: dispatch one task at a time "
                                "to the agy CLI and record the evidence.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--room", help="room name (under rooms/) or a path to a room folder; "
                                  "required for every command except `list`")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("list", help="list the projects (rooms) this tool knows")
    s = sub.add_parser("init", help="create a room for a repository")
    s.add_argument("--repo", required=True)
    s.add_argument("--base", default="main")
    s.add_argument("--agent", default=str(Path.home() / ".gemini" / "bin" / "agy.exe"))
    s = sub.add_parser("status", help="room state, open task, running round, problems")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("new", help="create a task and print the path of its task.md")
    s.add_argument("title")
    s = sub.add_parser("dispatch", help="run one round (blocking; run it as a background command)")
    s.add_argument("task")
    s = sub.add_parser("show", help="print the review packet of a task round")
    s.add_argument("task")
    s.add_argument("--round", type=int)
    s.add_argument("--diff", action="store_true", help="append the task's diff stat")
    s = sub.add_parser("verify", help="re-run the verify commands on the current tree")
    s.add_argument("task")
    s = sub.add_parser("rework", help="open the next round and its feedback.md")
    s.add_argument("task")
    s.add_argument("--override-cap", metavar="REASON")
    s = sub.add_parser("accept", help="squash-merge the task branch into the base branch")
    s.add_argument("task")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--skip-verify-gate", metavar="REASON",
                   help="accept without a passing verification of the branch head (logged)")
    s = sub.add_parser("reject", help="close the task without merging (the branch is kept)")
    s.add_argument("task")
    s.add_argument("--reason", default="")
    s.add_argument("--stash-leftovers", action="store_true")
    s = sub.add_parser("stop", help="kill the running round (it is recorded as INTERRUPTED)")
    s.add_argument("--reason", default="")
    s = sub.add_parser("pause", help="block new dispatches")
    s.add_argument("reason", nargs="?", default="")
    sub.add_parser("unpause", help="allow dispatches again")
    sub.add_parser("doctor", help="check the room, repo, agent and environment")
    return p


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command == "list":
        print("\n".join(cmd_list()))
        return 0
    if not args.room:
        print(f"error: --room is required for `{args.command}`", file=sys.stderr)
        return 2
    room_dir = resolve_room_dir(args.room)
    try:
        if args.command == "init":
            lines = cmd_init(room_dir, args)
        elif args.command == "doctor":
            code, lines = cmd_doctor(room_dir)
            print("\n".join(lines))
            return code
        else:
            lines = COMMANDS[args.command](Room.open(room_dir), args)
        print("\n".join(lines))
        return 0
    except RoomError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception:
        traceback.print_exc()
        return 1


# ---------------------------------------------------------------- read-only and setup commands

def cmd_init(room_dir: Path, args: argparse.Namespace) -> list[str]:
    if (room_dir / "room.toml").exists():
        raise Refused(f"a room already exists at {room_dir}")
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise UsageError(f"repo folder not found: {repo}")
    room_dir.mkdir(parents=True, exist_ok=True)
    text = store.read_text(TEMPLATES / "room.toml")
    for key, value in {"@@NAME@@": room_dir.name, "@@REPO@@": str(repo), "@@BASE@@": args.base,
                       "@@AGENT@@": args.agent}.items():
        text = text.replace(key, config.toml_str(value))
    store.atomic_write_text(room_dir / "room.toml", text)
    shutil.copyfile(TEMPLATES / "agy-settings.base.json", room_dir / "agy-settings.base.json")
    notes = store.read_text(TEMPLATES / "PROJECT-NOTES.md")
    store.atomic_write_text(room_dir / "PROJECT-NOTES.md",
                            notes.replace("@@NAME@@", room_dir.name).replace("@@REPO@@", str(repo)))
    store.write_json(room_dir / "state.json", new_room_state())
    Room.open(room_dir)
    store.append_event(room_dir, "room_created", repo=str(repo))
    return [f"room created: {room_dir}",
            "next: set verify.default_commands and policy.protected_paths in room.toml, "
            "fill in PROJECT-NOTES.md, then run `doctor`"]


def cmd_list() -> list[str]:
    rooms_dir = store.TOOL_HOME / "rooms"
    dirs = sorted(p for p in rooms_dir.iterdir() if (p / "room.toml").exists()) if rooms_dir.is_dir() else []
    rows = []
    for d in dirs:
        try:
            room = Room.open(d)
            st = room.load_state()
            flags = [f for f, on in (("running", st.get("running")), ("paused", room.paused is not None)) if on]
            rows.append(f"{d.name}: {room.repo} | open task {st.get('open_task') or '-'}"
                        + (f" | {', '.join(flags)}" if flags else ""))
        except RoomError as exc:
            rows.append(f"{d.name}: INVALID ({exc})")
    return rows or ["no rooms yet: run `room.py --room <name> init --repo <path>`"]


def status_data(room: Room) -> dict:
    st = room.load_state()
    lock_path = room.dir / lock.LOCK_NAME
    lock_info = None
    if lock_path.exists():
        lock_info = {"holder": lock.read_holder(lock_path), "alive": lock.holder_alive(lock_path)}
    problems = []
    running = st.get("running")
    if running:
        running = dict(running)
        running["dispatcher_alive"] = proc.is_alive(running.get("dispatcher_pid"), running.get("dispatcher_image"))
        running["agent_alive"] = proc.is_alive(running.get("agent_pid"), running.get("agent_image"))
        if not running["dispatcher_alive"]:
            problems.append("orphaned agent: the dispatcher is gone but the agent is still running; run `stop`"
                            if running["agent_alive"] else
                            "the dispatcher is gone; the next command will recover the round as INTERRUPTED")
    if lock_info and not lock_info["alive"]:
        problems.append("stale lock: its holder is gone")
    counts: dict[str, int] = {}
    for tid in room.task_ids():
        t = store.read_json(room.task_dir(tid) / "task-state.json", default={}) or {}
        counts[t.get("status", "?")] = counts.get(t.get("status", "?"), 0) + 1
    open_task = None
    if st.get("open_task"):
        t = room.load_task(st["open_task"])
        open_task = {"id": t["id"], "status": t["status"], "rounds": t["rounds"], "branch": t.get("branch")}
    repo = room.repo
    repo_branch = gitops.current_branch(repo) if repo.is_dir() and gitops.is_repo(repo) else None
    return {"room": room.config["project"]["name"], "dir": str(room.dir), "repo": str(repo),
            "repo_branch": repo_branch, "paused": room.paused, "open_task": open_task, "running": running,
            "lock": lock_info, "tasks": counts, "next_task_number": st["next_task_number"], "problems": problems}


def cmd_status(room: Room, args: argparse.Namespace) -> list[str]:
    d = status_data(room)
    if args.json:
        return [json.dumps(d, ensure_ascii=False, indent=2)]
    lines = [f"room: {d['room']} ({d['dir']})",
             f"repo: {d['repo']} (on {d['repo_branch'] or '?'})",
             "paused: " + ("no" if d["paused"] is None else f"yes {d['paused']}".strip())]
    t = d["open_task"]
    lines.append("open task: none" if not t else f"open task: {t['id']} {t['status']} (round {t['rounds']}, {t['branch']})")
    r = d["running"]
    lines.append("running: none" if not r else
                 f"running: {r['task']} round {r['round']} | dispatcher pid {r['dispatcher_pid']} "
                 f"({'alive' if r['dispatcher_alive'] else 'gone'}) | agent pid {r.get('agent_pid')} "
                 f"({'alive' if r['agent_alive'] else 'gone'}) | since {r['started_at']}")
    lk = d["lock"]
    lines.append("lock: free" if not lk else
                 f"lock: pid {(lk['holder'] or {}).get('pid')} `{(lk['holder'] or {}).get('command')}` "
                 f"({'alive' if lk['alive'] else 'stale'})")
    lines.append("tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(d["tasks"].items())) or "none"))
    lines += [f"PROBLEM: {p}" for p in d["problems"]]
    return lines


def cmd_show(room: Room, args: argparse.Namespace) -> list[str]:
    task = room.load_task(args.task)
    n = args.round or task["rounds"]
    lines = [f"{task['id']} {task['status']} | rounds {task['rounds']} | branch {task.get('branch') or '-'}"
             f" | {room.task_dir(task['id']) / 'task.md'}"]
    if n == 0:
        return lines + ["not dispatched yet"]
    rdir = room.round_dir(task["id"], n)
    run = store.read_json(rdir / "run.json")
    if run is None:
        return lines + [f"round {n}: no run.json yet (still running?)"]
    if run.get("run_status") == "ROOM_ERROR":
        lines.append(f"round {n}: ROOM_ERROR {run.get('error')}")
    else:
        lines += render.summary(run, str(rdir))
    report = rdir / "report.md"
    if report.exists():
        lines += ["", store.read_text(report).rstrip()]
    if args.diff and task.get("base_sha") and task.get("branch"):
        lines += ["", gitops.diff_stat(room.repo, task["base_sha"], task["branch"]) or "(no diff)"]
    return lines


def _antigravity_grants(repo: Path) -> dict | None:
    """Permission grants Antigravity saved for this repo (they merge into headless runs); None = no entry."""
    projects = Path.home() / ".gemini" / "config" / "projects"
    if not projects.is_dir():
        return None
    target = os.path.normcase(str(repo.resolve()))
    for f in projects.glob("*.json"):
        try:
            data = store.read_json(f, default={}) or {}
        except (ValueError, OSError):
            continue
        for res in (data.get("projectResources") or {}).get("resources") or []:
            path = urllib.parse.unquote(urllib.parse.urlparse(res.get("folderUri") or "").path).lstrip("/")
            if path and os.path.normcase(str(Path(path).resolve())) == target:
                grants = data.get("permissionGrants") or {}
                return {k: v for k, v in grants.items() if k != "v2Migrated" and v}
    return None


def cmd_doctor(room_dir: Path) -> tuple[int, list[str]]:
    results: list[tuple[str, str]] = []

    def add(level: str, text: str) -> None:
        results.append((level, text))

    def finish() -> tuple[int, list[str]]:
        code = 2 if any(level == "FAIL" for level, _ in results) else 0
        return code, [f"[{level}] {text}" for level, text in results]

    add("OK" if sys.version_info >= (3, 11) else "FAIL", f"python {sys.version.split()[0]}")
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        add("OK", out)
    except (OSError, subprocess.TimeoutExpired):
        add("FAIL", "git not found")
    try:
        room = Room.open(room_dir)
        add("OK", f"room.toml valid ({room_dir})")
    except RoomError as exc:
        add("FAIL", str(exc))
        return finish()
    cfg, repo = room.config, room.repo
    if not repo.is_dir() or not gitops.is_repo(repo):
        add("FAIL", f"repo is not a git repository: {repo}")
    else:
        add("OK", f"repo {repo} (on {gitops.current_branch(repo) or 'a detached HEAD'})")
        base = cfg["project"]["base_branch"]
        add("OK" if gitops.branch_exists(repo, base) else "FAIL", f"base branch {base}")
        dirty = gitops.dirty_entries(repo)
        add("OK" if not dirty else "WARN", "working tree clean" if not dirty else
            f"working tree not clean ({len(dirty)}): {'; '.join(dirty[:5])} - dispatch will refuse")
        operation = gitops.operation_in_progress(repo)
        if operation:
            add("WARN", f"a git {operation} is in progress")
    command = cfg["agent"]["command"]
    if not Path(command).exists():
        add("FAIL", f"agent command not found: {command}")
    else:
        try:
            version = agent.agent_version(cfg["agent"])
            tested = cfg["agent"]["tested_version"]
            same = not tested or version == tested
            add("OK" if same else "WARN", f"agent {version}" + ("" if same else
                f" (tested with {tested}; rerun acceptance tests A1-A2)"))
        except RoomError as exc:
            add("FAIL", str(exc))
    model = cfg["agent"]["permission_model"]
    try:
        s = agent.build_settings(store.read_text(room.dir / cfg["agent"]["settings_file"]), repo, [], [], model)
        add("OK", f"agent settings valid ({len(s['permissions']['allow'])} allow, "
                  f"{len(s['permissions']['deny'])} deny rules)")
    except (OSError, RoomError) as exc:
        add("FAIL", f"agent settings: {exc}")
    if model == "sandbox":
        add("OK", "permission model: sandbox (every agent command stays in the sandbox; a bypass ends the round)")
    else:
        add("WARN", "permission model: allowlist (the agent can run allowed commands outside the sandbox, F17)")
    room_run = cfg["verify"]["room_run"]
    add("OK" if room_run == "after_review" else "INFO",
        f"verify.room_run: {room_run}" + (" (The Foreman runs agent code unsandboxed only after review)"
                                          if room_run == "after_review" else ""))
    grants = _antigravity_grants(repo)
    if grants is None:
        add("OK", "no Antigravity project entry for this repo")
    elif grants:
        add("WARN", "Antigravity has saved permission grants for this repo; they widen headless runs: "
            + json.dumps(grants, ensure_ascii=False)[:200])
    else:
        add("OK", "Antigravity project entry has no permission grants")
    if not cfg["verify"]["default_commands"]:
        add("WARN", "verify.default_commands is empty: tasks without `verify` get no verification")
    if proc.antigravity_ide_running():
        add("INFO", "Antigravity IDE is running: keep its agent idle on this repo during dispatches")
    if room.paused is not None:
        add("INFO", "room is paused")
    for problem in status_data(room)["problems"]:
        add("WARN", problem)
    return finish()


# ---------------------------------------------------------------- command table

def _new(room: Room, args: argparse.Namespace) -> list[str]:
    tid, path = lifecycle.new_task(room, args.title)
    return [f"{tid} created: {path}", f"edit task.md, then run: dispatch {tid} (as a background command)"]


def _rework(room: Room, args: argparse.Namespace) -> list[str]:
    path = lifecycle.rework(room, args.task, args.override_cap)
    return [f"{args.task} is ready for rework: write {path}, then run: dispatch {args.task}"]


COMMANDS = {
    "status": cmd_status,
    "show": cmd_show,
    "new": _new,
    "dispatch": lambda room, a: dispatch.dispatch(room, a.task),
    "verify": lambda room, a: lifecycle.manual_verify(room, a.task),
    "rework": _rework,
    "accept": lambda room, a: lifecycle.accept(room, a.task, a.message, a.skip_verify_gate),
    "reject": lambda room, a: lifecycle.reject(room, a.task, a.reason, a.stash_leftovers),
    "stop": lambda room, a: lifecycle.stop(room, a.reason),
    "pause": lambda room, a: lifecycle.pause(room, a.reason),
    "unpause": lambda room, a: lifecycle.unpause(room),
}
