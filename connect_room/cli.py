"""Command-line interface: argument parsing, exit codes, and the read-only commands."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.parse
from pathlib import Path

from . import __version__, agent, config, dispatch, gitops, lifecycle, lock, proc, render, store
from .errors import RoomError, UsageError, Refused
from .state import Room, new_room_state, resolve_room_dir

TEMPLATES = store.TOOL_HOME / "templates"
DEFAULT_AGENT = str(Path.home() / ".gemini" / "bin" / "agy.exe")
MIN_PYTHON, MIN_GIT = (3, 11), (2, 40)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="room.py", description="The Foreman: dispatch one task at a time "
                                "to the agy CLI and record the evidence.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--room", help="room name (under rooms/) or a path to a room folder; "
                                  "required for every command except `list`")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")
    s = sub.add_parser("list", help="list the projects (rooms) this tool knows")
    s.add_argument("--json", action="store_true", help="every room's status and latest round evidence")
    s.add_argument("--rooms-dir", type=Path, help="look for rooms here instead of the tool's rooms/ folder")
    s = sub.add_parser("check", help="check this machine (python, git, agent) without a room")
    s.add_argument("--agent", default=DEFAULT_AGENT)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("init", help="create a room for a repository")
    s.add_argument("--repo", required=True)
    s.add_argument("--base", default="main")
    s.add_argument("--agent", default=DEFAULT_AGENT)
    s.add_argument("--verify", action="append", default=[], metavar="CMD",
                   help="a verify.default_commands entry (repeatable)")
    s.add_argument("--protect", action="append", default=[], metavar="PATH",
                   help="a policy.protected_paths entry (repeatable)")
    s.add_argument("--deny", action="append", default=[], metavar="RULE",
                   help="an extra executor deny rule for agy-settings.base.json (repeatable)")
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
    s.add_argument("--json", action="store_true", help="round evidence and report as JSON")
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
    s = sub.add_parser("doctor", help="check the room, repo, agent and environment")
    s.add_argument("--json", action="store_true", help="results with a fix hint each")
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
        print(json.dumps(list_data(args.rooms_dir), ensure_ascii=False, indent=2) if args.json
              else "\n".join(cmd_list(args.rooms_dir)))
        return 0
    if args.command == "check":
        results = machine_checks(args.agent)
        print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else "\n".join(fmt_results(results)))
        return results_code(results)
    if not args.room:
        print(f"error: --room is required for `{args.command}`", file=sys.stderr)
        return 2
    room_dir = resolve_room_dir(args.room)
    try:
        if args.command == "init":
            lines = cmd_init(room_dir, args)
        elif args.command == "doctor":
            results = doctor_results(room_dir)
            print(json.dumps(results, ensure_ascii=False, indent=2) if args.json
                  else "\n".join(fmt_results(results)))
            return results_code(results)
        elif args.command == "show" and args.json:
            print(json.dumps(show_data(Room.open(room_dir), args.task, args.round), ensure_ascii=False, indent=2))
            return 0
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
    verify_cmds = _clean_list(getattr(args, "verify", []), "--verify")
    protected = _clean_list(getattr(args, "protect", []), "--protect")
    deny = _clean_list(getattr(args, "deny", []), "--deny")
    text = store.read_text(TEMPLATES / "room.toml")
    for key, value in {"@@NAME@@": room_dir.name, "@@REPO@@": str(repo), "@@BASE@@": args.base,
                       "@@AGENT@@": args.agent}.items():
        text = text.replace(key, config.toml_str(value))
    text = _set_list(text, "default_commands", verify_cmds)
    text = _set_list(text, "protected_paths", protected)
    settings_text = store.read_text(TEMPLATES / "agy-settings.base.json")
    if deny:
        settings = json.loads(settings_text)
        rules = settings["permissions"]["deny"]
        rules += [r for r in deny if r not in rules]
        settings_text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    room_dir.mkdir(parents=True, exist_ok=True)
    store.atomic_write_text(room_dir / "room.toml", text)
    store.atomic_write_text(room_dir / "agy-settings.base.json", settings_text)
    notes = store.read_text(TEMPLATES / "PROJECT-NOTES.md")
    store.atomic_write_text(room_dir / "PROJECT-NOTES.md",
                            notes.replace("@@NAME@@", room_dir.name).replace("@@REPO@@", str(repo)))
    store.write_json(room_dir / "state.json", new_room_state())
    Room.open(room_dir)
    store.append_event(room_dir, "room_created", repo=str(repo))
    todo = [key for key, done in (("verify.default_commands", verify_cmds),
                                  ("policy.protected_paths", protected)) if not done]
    return [f"room created: {room_dir}",
            "next: " + (f"set {' and '.join(todo)} in room.toml, " if todo else "")
            + "fill in PROJECT-NOTES.md, then run `doctor`"]


def _clean_list(values: list[str], flag: str) -> list[str]:
    out: list[str] = []
    for value in values:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise UsageError(f"{flag} needs a non-empty, single-line value")
        if value not in out:
            out.append(value)
    return out


def _set_list(text: str, key: str, values: list[str]) -> str:
    """Fill the template's one-line `key = []` with the given strings."""
    if not values:
        return text
    marker = re.compile(rf"^({re.escape(key)}\s*=\s*)\[\]", re.M)
    if not marker.search(text):
        raise UsageError(f"template room.toml has no empty `{key}` list")
    rendered = "[" + ", ".join(config.toml_str(v) for v in values) + "]"
    return marker.sub(lambda m: m.group(1) + rendered, text, count=1)


def cmd_list(rooms_dir: Path | None = None) -> list[str]:
    rooms_dir = rooms_dir or store.TOOL_HOME / "rooms"
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


def list_data(rooms_dir: Path | None = None) -> list[dict]:
    """Every room with its status and the evidence of its latest round (for dashboards)."""
    rooms_dir = rooms_dir or store.TOOL_HOME / "rooms"
    dirs = sorted(p for p in rooms_dir.iterdir() if (p / "room.toml").exists()) if rooms_dir.is_dir() else []
    rows = []
    for d in dirs:
        row: dict = {"name": d.name, "dir": str(d), "status": None, "latest": None, "error": None}
        try:
            room = Room.open(d)
            row["status"] = status_data(room)
            row["max_rounds"] = room.config["policy"]["max_rounds"]
            row["timeout_minutes"] = room.config["agent"]["timeout_minutes"]
            row["base_branch"] = room.config["project"]["base_branch"]
            ids = room.task_ids()
            if ids:
                row["latest"] = show_data(room, ids[-1], None)
        except RoomError as exc:
            row["error"] = str(exc)
        except (OSError, ValueError) as exc:
            row["error"] = f"unreadable room: {exc}"
        rows.append(row)
    return rows


RUN_KEYS = ("run_status", "result_status", "agent_status", "verdict_hint", "warnings", "notices",
            "claim_mismatches", "duration_s", "open_issues_count", "questions_count")


def show_data(room: Room, tid: str, round_no: int | None) -> dict:
    task = room.load_task(tid)
    try:
        title = str(room.task_meta(tid)[0].get("title") or "")
    except (RoomError, OSError, ValueError):
        title = ""
    n = round_no or task["rounds"]
    history = task.get("history") or []
    out: dict = {"task": task["id"], "title": title, "status": task["status"], "rounds": task["rounds"],
                 "branch": task.get("branch"), "updated_at": history[-1].get("ts") if history else None, "task_dir": str(room.task_dir(task["id"])), "round": n,
                 "round_dir": None, "run": None, "report": None}
    if n == 0:
        return out
    rdir = room.round_dir(task["id"], n)
    out["round_dir"] = str(rdir)
    run = store.read_json(rdir / "run.json")
    if run is not None:
        summary = {k: run.get(k) for k in RUN_KEYS}
        if run.get("run_status") == "ROOM_ERROR":
            summary["error"] = run.get("error")
        checks, verify = run.get("checks") or {}, run.get("verify") or {}
        summary["checks"] = {k: checks.get(k) or [] for k in
                             ("scope_violations", "protected_paths_touched", "sensitive_files")}
        summary["verify"] = {"status": verify.get("status"), "commands": [
            {k: c.get(k) for k in ("command", "agent_exit_code", "room_exit_code", "timed_out")}
            for c in verify.get("commands") or []]}
        out["run"] = summary
    report = rdir / "report.md"
    if report.exists():
        out["report"] = store.read_text(report)
    return out


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


LINKS = {"python": "https://www.python.org/downloads/", "git": "https://git-scm.com/downloads",
         "agent": "https://antigravity.google/"}


class _Results(list):
    """Check results: dicts with level (OK, INFO, WARN, FAIL), check id, text, a plain fix hint and a link."""

    def add(self, level: str, check: str, text: str, hint: str = "", url: str = "") -> None:
        self.append({"level": level, "check": check, "text": text, "hint": hint, "url": url})


def fmt_results(results: list[dict]) -> list[str]:
    lines = []
    for r in results:
        lines.append(f"[{r['level']}] {r['text']}")
        if r.get("hint") and r["level"] in ("WARN", "FAIL"):
            lines.append(f"       fix: {r['hint']}" + (f" ({r['url']})" if r.get("url") else ""))
    return lines


def results_code(results: list[dict]) -> int:
    return 2 if any(r["level"] == "FAIL" for r in results) else 0


def _version_tuple(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x) for x in m.groups() if x is not None) if m else ()


def _template_tested_version() -> str:
    m = re.search(r'^tested_version\s*=\s*"([^"]*)"', store.read_text(TEMPLATES / "room.toml"), re.M)
    return m.group(1) if m else ""


def _check_tools(res: _Results) -> None:
    py = sys.version.split()[0]
    res.add("OK" if sys.version_info >= MIN_PYTHON else "FAIL", "python", f"python {py}",
            "" if sys.version_info >= MIN_PYTHON else "Install Python 3.11 or newer, then start The Foreman with it",
            "" if sys.version_info >= MIN_PYTHON else LINKS["python"])
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    if not out:
        res.add("FAIL", "git", "git not found", "Install Git for Windows, then open a new terminal", LINKS["git"])
    elif _version_tuple(out) < MIN_GIT:
        res.add("WARN", "git", out, "Git 2.40 or newer is recommended: update Git for Windows", LINKS["git"])
    else:
        res.add("OK", "git", out)


def _check_agent(res: _Results, agent_cfg: dict) -> None:
    command = agent_cfg["command"]
    if not Path(command).exists():
        res.add("FAIL", "agent", f"agent command not found: {command}",
                "Install the Antigravity CLI (agy), or point the room's agent.command at agy.exe", LINKS["agent"])
        return
    try:
        version = agent.agent_version(agent_cfg)
    except RoomError as exc:
        res.add("FAIL", "agent", str(exc), "Run `agy --version` in a terminal to see why it fails", LINKS["agent"])
        return
    tested = agent_cfg.get("tested_version") or ""
    same = not tested or version == tested
    res.add("OK" if same else "WARN", "agent_version", f"agent {version}" + ("" if same else
            f" (tested with {tested}; rerun acceptance tests A1-A2)"),
            "" if same else "It probably works; rerun the acceptance tests in docs/ACCEPTANCE-TESTS.md to be sure")


def machine_checks(agent_command: str = DEFAULT_AGENT) -> list[dict]:
    """Checks that need no room: Python, Git, the agent binary and its version."""
    res = _Results()
    _check_tools(res)
    _check_agent(res, {"command": agent_command, "args_prefix": [], "tested_version": _template_tested_version()})
    res.add("INFO", "agent_sign_in", "agy sign-in is not checked (every known check costs a model call)",
            "If agy has never run on this machine, run `agy` once in a terminal and sign in")
    if proc.antigravity_ide_running():
        res.add("INFO", "ide", "Antigravity IDE is running: keep its agent idle on a repo during dispatches")
    return res


def doctor_results(room_dir: Path) -> list[dict]:
    res = _Results()
    _check_tools(res)
    try:
        room = Room.open(room_dir)
        res.add("OK", "room", f"room.toml valid ({room_dir})")
    except RoomError as exc:
        res.add("FAIL", "room", str(exc), "Fix the reported key in room.toml (unknown keys are rejected)")
        return res
    cfg, repo = room.config, room.repo
    if not repo.is_dir() or not gitops.is_repo(repo):
        res.add("FAIL", "repo", f"repo is not a git repository: {repo}",
                "Point project.repo in room.toml at the project's git working copy")
    else:
        res.add("OK", "repo", f"repo {repo} (on {gitops.current_branch(repo) or 'a detached HEAD'})")
        base = cfg["project"]["base_branch"]
        ok = gitops.branch_exists(repo, base)
        res.add("OK" if ok else "FAIL", "base_branch", f"base branch {base}",
                "" if ok else "Set project.base_branch in room.toml to a branch that exists")
        dirty = gitops.dirty_entries(repo)
        res.add("OK" if not dirty else "WARN", "clean_tree", "working tree clean" if not dirty else
                f"working tree not clean ({len(dirty)}): {'; '.join(dirty[:5])} - dispatch will refuse",
                "" if not dirty else "Commit or stash your changes in the project before a dispatch")
        operation = gitops.operation_in_progress(repo)
        if operation:
            res.add("WARN", "git_operation", f"a git {operation} is in progress",
                    f"Finish or abort the {operation} in the project first")
    _check_agent(res, cfg["agent"])
    model = cfg["agent"]["permission_model"]
    try:
        s = agent.build_settings(store.read_text(room.dir / cfg["agent"]["settings_file"]), repo, [], [], model)
        res.add("OK", "settings", f"agent settings valid ({len(s['permissions']['allow'])} allow, "
                                  f"{len(s['permissions']['deny'])} deny rules)")
    except (OSError, RoomError) as exc:
        res.add("FAIL", "settings", f"agent settings: {exc}",
                f"Fix {cfg['agent']['settings_file']} in the room folder (it must be valid JSON)")
    if model == "sandbox":
        res.add("OK", "permission_model",
                "permission model: sandbox (every agent command stays in the sandbox; a bypass ends the round)")
    else:
        res.add("WARN", "permission_model",
                "permission model: allowlist (the agent can run allowed commands outside the sandbox, F17)",
                "Use agent.permission_model = \"sandbox\" unless you have a documented reason")
    room_run = cfg["verify"]["room_run"]
    res.add("OK" if room_run == "after_review" else "INFO", "room_run",
            f"verify.room_run: {room_run}" + (" (The Foreman runs agent code unsandboxed only after review)"
                                              if room_run == "after_review" else ""))
    grants = _antigravity_grants(repo)
    if grants is None:
        res.add("OK", "grants", "no Antigravity project entry for this repo")
    elif grants:
        res.add("WARN", "grants", "Antigravity has saved permission grants for this repo; they widen headless runs: "
                + json.dumps(grants, ensure_ascii=False)[:200],
                "Remove the saved grants for this folder in the Antigravity app's permission settings")
    else:
        res.add("OK", "grants", "Antigravity project entry has no permission grants")
    if not cfg["verify"]["default_commands"]:
        res.add("WARN", "verify_commands",
                "verify.default_commands is empty: tasks without `verify` get no verification",
                "Add the commands that prove work (e.g. your test command) to verify.default_commands")
    if proc.antigravity_ide_running():
        res.add("INFO", "ide", "Antigravity IDE is running: keep its agent idle on this repo during dispatches")
    if room.paused is not None:
        res.add("INFO", "paused", "room is paused")
    for problem in status_data(room)["problems"]:
        res.add("WARN", "problem", problem, "Run `status`; it recovers an interrupted round")
    return res


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
