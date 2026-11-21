"""One dispatch round (DESIGN.md §8): preflight, launch agy, parse its stream, snapshot, check,
verify, record. Claude runs it as a background command; it blocks until the round is recorded."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import __version__, agent, gitops, lifecycle, proc, render, rounds, store, verify
from .errors import ConfigError, Refused
from .state import Room, transition

SCHEMA_PATH = store.TOOL_HOME / "templates" / "result.schema.json"
PREAMBLE_PATH = store.TOOL_HOME / "prompts" / "gemini-preamble.md"


def dispatch(room: Room, tid: str) -> list[str]:
    with lifecycle.locked(room, f"dispatch {tid}"):
        ctx = _prepare(room, tid)
        return _run_round(room, ctx)


def _prepare(room: Room, tid: str) -> dict:
    cfg, repo = room.config, room.repo
    if room.paused is not None:
        raise Refused("room is paused" + (f": {room.paused}" if room.paused else "") + " (run `unpause`)")
    task = room.load_task(tid)
    open_task = room.load_state().get("open_task")
    if open_task not in (None, tid):
        raise Refused(f"task {open_task} is open; accept or reject it first")
    if task["status"] not in ("DRAFT", "REWORK_READY"):
        raise Refused(f"{tid} is {task['status']}; dispatch needs DRAFT or REWORK_READY")
    meta, body = room.task_meta(tid)
    n = task["rounds"] + 1
    max_rounds = meta.get("max_rounds", cfg["policy"]["max_rounds"])
    if n > max_rounds and task.get("round_cap_override", 0) < n:
        raise Refused(f"{tid} reached its {max_rounds}-round cap (see `rework --override-cap`)")

    if not repo.is_dir() or not gitops.is_repo(repo):
        raise ConfigError(f"repo is not a git repository: {repo}")
    if not Path(cfg["agent"]["command"]).exists():
        raise ConfigError(f"agent command not found: {cfg['agent']['command']}")
    version = agent.agent_version(cfg["agent"])
    notices = []
    tested = cfg["agent"]["tested_version"]
    if tested and version != tested:
        notices.append(f"AGENT_VERSION_CHANGED ({version}; tested {tested})")
    if proc.antigravity_ide_running():
        notices.append("ANTIGRAVITY_IDE_RUNNING")

    gitops.settle_stat_dirty(repo)
    dirty = gitops.dirty_entries(repo)
    if dirty:
        raise Refused("working tree is not clean: " + "; ".join(dirty[:10]) + (" ..." if len(dirty) > 10 else ""))
    operation = gitops.operation_in_progress(repo)
    if operation:
        raise Refused(f"a git {operation} is in progress in the repo")
    branch = cfg["project"]["branch_prefix"] + tid
    base_branch = cfg["project"]["base_branch"]
    if n == 1:
        if not gitops.branch_exists(repo, base_branch):
            raise ConfigError(f"base branch {base_branch} not found")
        if gitops.branch_exists(repo, branch):
            raise Refused(f"branch {branch} already exists")
    elif gitops.current_branch(repo) != branch:
        raise Refused(f"HEAD must be on {branch} for round {n}")

    verify_cmds = meta["verify"] if "verify" in meta else list(cfg["verify"]["default_commands"])
    model = cfg["agent"]["permission_model"]
    settings = agent.build_settings(store.read_text(room.dir / cfg["agent"]["settings_file"]), repo,
                                    verify_cmds, meta.get("extra_allow", []), model)
    rdir = room.round_dir(tid, n)
    feedback = previous = None
    if n > 1:
        fb = rdir / "feedback.md"
        feedback = store.read_text(fb) if fb.exists() else ""
        if not feedback.strip() or feedback.strip() == store.read_text(lifecycle.FEEDBACK_TEMPLATE).strip():
            raise Refused(f"write the feedback in {fb} before dispatching round {n}")
        previous = _previous_summary(room, tid, n - 1)
    prompt = agent.build_prompt(store.read_text(PREAMBLE_PATH), tid, n, branch, meta, body, verify_cmds,
                                settings["permissions"]["allow"], feedback, previous, model)
    limit = cfg["agent"]["max_prompt_chars"]
    if len(prompt) > limit:
        raise Refused(f"the prompt is {len(prompt)} characters; the limit is {limit}. "
                      "Shorten the task or point to repo files instead of pasting them")
    return {"task": task, "meta": meta, "n": n, "branch": branch, "base_branch": base_branch,
            "verify_cmds": verify_cmds, "settings": settings, "prompt": prompt, "rdir": rdir,
            "timeout_min": meta.get("timeout_minutes", cfg["agent"]["timeout_minutes"]),
            "version": version, "notices": notices}


def _previous_summary(room: Room, tid: str, n: int) -> str:
    run = store.read_json(room.round_dir(tid, n) / "run.json", default={}) or {}
    res = store.read_json(room.round_dir(tid, n) / "result.json", default={}) or {}
    verify_part = run.get("verify") or {}
    lines = [f"Round {n}: run {run.get('run_status')}, your status {run.get('agent_status')}, "
             f"verify {verify_part.get('status')}, warnings: {', '.join(run.get('warnings') or []) or 'none'}"]
    if isinstance(res, dict) and res.get("summary"):
        lines.append(f"Your summary then: {res['summary']}")
    for c in verify_part.get("commands") or []:
        lines.append(f"- `{c['command']}`: your run exit {c.get('agent_exit_code')}, "
                     f"The Foreman run exit {c.get('room_exit_code')}")
    aborted = (run.get("activity") or {}).get("aborted_on")
    if aborted:
        lines.append(f"That round ended because this was not allowed: {aborted['tool']} {aborted['target']}")
    return "\n".join(lines)


def _run_round(room: Room, c: dict) -> list[str]:
    repo, task, n, rdir = room.repo, c["task"], c["n"], c["rdir"]
    tid = task["id"]
    rdir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        (room.dir / lifecycle.STOP_MARKER).unlink()
    if n == 1:
        task["base_sha"] = gitops.rev(repo, c["base_branch"])
        gitops.create_branch(repo, c["branch"], c["base_branch"])
        task["branch"] = c["branch"]
    head_before = gitops.rev(repo)
    store.atomic_write_text(rdir / "prompt.md", c["prompt"])
    settings_path = rdir / "agy-settings.json"
    store.write_json(settings_path, c["settings"])
    task["rounds"] = n
    transition(task, "RUNNING", f"round {n}")
    room.save_task(task)
    st = room.load_state()
    st["open_task"] = tid
    st["running"] = {"task": tid, "round": n, "dispatcher_pid": os.getpid(),
                     "dispatcher_image": sys.executable, "agent_pid": None,
                     "agent_image": room.config["agent"]["command"], "started_at": store.now_iso(),
                     "head_before": head_before}
    room.save_state(st)
    room.event("dispatch_started", tid, n, branch=c["branch"], head_before=head_before)
    try:
        return _launch_and_record(room, c, head_before, settings_path)
    except BaseException as exc:
        _fail_round(room, c, exc)
        raise


def _launch_and_record(room: Room, c: dict, head_before: str, settings_path: Path) -> list[str]:
    cfg, repo, task, n, rdir = room.config, room.repo, c["task"], c["n"], c["rdir"]
    tid, acfg = task["id"], cfg["agent"]
    conversation = task.get("last_conversation_id") if n > 1 else None
    argv = agent.build_argv(acfg, c["prompt"], SCHEMA_PATH, settings_path, c["timeout_min"], conversation)
    env = agent.agent_env(dict(os.environ), acfg["env_scrub"], acfg["env_passthrough"])
    out_path, err_path = rdir / "agy-stdout.jsonl", rdir / "agy-stderr.log"
    marker = room.dir / lifecycle.STOP_MARKER
    started_at, t0 = store.now_iso(), time.monotonic()
    timeout_s = c["timeout_min"] * 60
    deadline = t0 + timeout_s + min(90.0, 0.25 * timeout_s)  # agy's own --print-timeout should fire first
    timed_out = False
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        p = subprocess.Popen(argv, cwd=repo, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                             **proc.popen_kwargs())
        try:
            st = room.load_state()
            st["running"]["agent_pid"] = p.pid
            room.save_state(st)
            room.event("agent_started", tid, n, pid=p.pid)
            while p.poll() is None:
                if marker.exists():
                    proc.kill_tree(p.pid)
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    proc.kill_tree(p.pid)
                    break
                time.sleep(0.5)
            exit_code = p.wait()
        finally:
            if p.poll() is None:
                proc.kill_tree(p.pid)
                p.wait()
    stop_reason = None
    if marker.exists():
        stop_reason = store.read_text(marker).strip() or "stop requested"
        marker.unlink()
    duration = round(time.monotonic() - t0, 1)

    parsed = agent.parse_stream(out_path.read_bytes().decode("utf-8", "replace"), repo)
    stderr_text = err_path.read_bytes().decode("utf-8", "replace")
    res = agent.extract_result(parsed)
    if timed_out:
        run_status = "TIMED_OUT"
    elif stop_reason is not None:
        run_status = "INTERRUPTED"
    elif exit_code != 0:
        run_status = "CRASHED"
    elif parsed["aborted_on"]:
        run_status = "ABORTED_PERMISSION"
    else:
        run_status = "COMPLETED"

    warnings: list[str] = []
    notices = list(c["notices"])
    if "--settings must be" in stderr_text:
        warnings.append("AGENT_SETTINGS_REJECTED")
    envelope = parsed["result"] or {}
    if run_status == "COMPLETED" and (parsed["result"] is None or envelope.get("error")
                                      or envelope.get("status") not in (None, "SUCCESS")):
        warnings.append("AGENT_ERROR")
    if parsed["bad_lines"]:
        notices.append(f"STREAM_BAD_LINES ({parsed['bad_lines']})")
    if any(r["kind"] == "rule" for r in parsed["refused"]):
        warnings.append("DENIED_BY_RULE")
    data = res["data"] if res["status"] == "VALID" else None
    agent_status = data.get("status") if data else None

    git_part = rounds.capture(room, task, n, head_before, c["meta"].get("allowed_paths") or [],
                              c["meta"]["title"], run_status)
    warnings += git_part["warnings"]
    mismatches = verify.claim_mismatches(data.get("commands_run", []) if data else [], parsed["commands"])
    if mismatches:
        warnings.append("CLAIM_MISMATCH")
    evidence = verify.agent_evidence(c["verify_cmds"], parsed)
    eligible = (run_status == "COMPLETED" and agent_status == "DONE"
                and git_part["git"]["snapshot"] in ("COMMITTED", "NO_CHANGES"))
    room_run = cfg["verify"]["room_run"]
    room_results = []
    if eligible and room_run == "after_round" and c["verify_cmds"]:
        room_results = verify.run_room(c["verify_cmds"], repo, cfg["verify"]["timeout_minutes"], rdir)
    verify_part = verify.combine(c["verify_cmds"], evidence, room_results, eligible, room_run)
    verify_part["head"] = gitops.rev(repo)
    if not c["verify_cmds"]:
        warnings.append("VERIFY_SKIPPED")
    ready = (run_status == "COMPLETED" and res["status"] == "VALID" and agent_status == "DONE"
             and git_part["git"]["snapshot"] in ("COMMITTED", "NO_CHANGES")
             and verify_part["status"] in ("PASSED", "AWAITING_ROOM_RUN") and not warnings)

    run = {
        "schema": 1, "task_id": tid, "round": n, "room_version": __version__, "title": c["meta"]["title"],
        "base_branch": c["base_branch"], "started_at": started_at, "ended_at": store.now_iso(),
        "duration_s": duration,
        "agent": {
            "command": acfg["command"], "version": c["version"],
            "version_matches_tested": not acfg["tested_version"] or c["version"] == acfg["tested_version"],
            "mode": acfg["mode"], "sandbox": acfg["sandbox"], "permission_model": acfg["permission_model"],
            "model": acfg["model"] or None,
            "exit_code": exit_code, "conversation_id": envelope.get("conversation_id") or parsed["conversation_id"],
            "status": envelope.get("status"), "error": envelope.get("error"),
            "denied_actions": parsed["denied_actions"], "usage": envelope.get("usage"), "stop_reason": stop_reason,
        },
        "run_status": run_status, "result_status": res["status"], "result_problems": res["problems"],
        "agent_status": agent_status,
        "activity": {"commands": parsed["commands"], "files_written": parsed["files_written"],
                     "refused": parsed["refused"], "aborted_on": parsed["aborted_on"]},
        "git": git_part["git"], "checks": git_part["checks"], "verify": verify_part,
        "claim_mismatches": mismatches, "warnings": warnings, "notices": notices,
        "result_summary": (data or {}).get("summary", ""),
        "open_issues_count": len((data or {}).get("open_issues", [])),
        "questions_count": len((data or {}).get("questions", [])),
        "verdict_hint": "READY_FOR_REVIEW" if ready else "NEEDS_ATTENTION",
    }
    store.write_json(rdir / "result.json", res["data"] if res["data"] is not None else {"raw": res["raw"]})
    store.atomic_write_text(rdir / "report.md", render.report_md(tid, n, res, run))
    store.write_json(rdir / "run.json", run)
    task["last_conversation_id"] = run["agent"]["conversation_id"] or task.get("last_conversation_id")
    transition(task, "AWAITING_REVIEW", f"round {n}: {run_status}")
    room.save_task(task)
    st = room.load_state()
    st["running"] = None
    room.save_state(st)
    room.event("round_recorded", tid, n, run_status=run_status, verdict=run["verdict_hint"])
    return render.summary(run, str(rdir))


def _fail_round(room: Room, c: dict, exc: BaseException) -> None:
    """Leave a consistent state when room.py itself fails mid-round (the round becomes reviewable)."""
    tid, n = c["task"]["id"], c["n"]
    with contextlib.suppress(Exception):
        running = room.load_state().get("running") or {}
        if proc.is_alive(running.get("agent_pid"), running.get("agent_image")):
            proc.kill_tree(running["agent_pid"])
    run = {"schema": 1, "task_id": tid, "round": n, "room_version": __version__, "run_status": "ROOM_ERROR",
           "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-4000:],
           "result_status": "MISSING", "agent_status": None, "warnings": ["ROOM_ERROR"], "notices": [],
           "verdict_hint": "NEEDS_ATTENTION"}
    with contextlib.suppress(Exception):
        store.write_json(c["rdir"] / "run.json", run)
    with contextlib.suppress(Exception):
        task = room.load_task(tid)
        if task["status"] == "RUNNING":
            transition(task, "AWAITING_REVIEW", f"round {n}: room error")
            room.save_task(task)
        st = room.load_state()
        st["running"] = None
        room.save_state(st)
        room.event("round_failed", tid, n, error=str(exc)[:500])
