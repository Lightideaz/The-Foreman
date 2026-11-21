"""Lifecycle commands: new, rework, accept, reject, stop, pause, manual verify, and recovery."""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

from . import __version__, agent, gitops, lock, proc, rounds, store, verify
from .config import toml_str
from .errors import Refused, UsageError
from .state import Room, transition

TASK_TEMPLATE = store.TOOL_HOME / "templates" / "task.md"
FEEDBACK_TEMPLATE = store.TOOL_HOME / "templates" / "feedback.md"
STOP_MARKER = "STOP_REQUESTED"


def locked(room: Room, command: str):
    return lock.locked(room.dir, command, on_stale=lambda holder: recover(room, holder))


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def new_task(room: Room, title: str) -> tuple[str, Path]:
    title = title.strip()
    if not title:
        raise UsageError("the title must not be empty")
    with locked(room, "new"):
        st = room.load_state()
        number = st["next_task_number"]
        tid = f"T-{number:04d}"
        st["next_task_number"] = number + 1
        room.save_state(st)
        tdir = room.task_dir(tid)
        tdir.mkdir(parents=True, exist_ok=False)
        text = (store.read_text(TASK_TEMPLATE).replace("@@TITLE@@", toml_str(title))
                .replace("@@TIMEOUT@@", _fmt_number(room.config["agent"]["timeout_minutes"])))
        store.atomic_write_text(tdir / "task.md", text)
        room.save_task({
            "schema": 1, "id": tid, "title": title, "status": "DRAFT", "rounds": 0, "branch": None,
            "base_sha": None, "created_at": store.now_iso(), "last_conversation_id": None,
            "history": [{"ts": store.now_iso(), "status": "DRAFT", "note": "created"}],
        })
        room.event("task_created", tid, title=title)
    return tid, tdir / "task.md"


def rework(room: Room, tid: str, override_reason: str | None = None) -> Path:
    with locked(room, f"rework {tid}"):
        task = room.load_task(tid)
        if task["status"] != "AWAITING_REVIEW":
            raise Refused(f"{tid} is {task['status']}; rework needs AWAITING_REVIEW")
        meta, _ = room.task_meta(tid)
        max_rounds = meta.get("max_rounds", room.config["policy"]["max_rounds"])
        nxt = task["rounds"] + 1
        if nxt > max_rounds:
            if not override_reason:
                raise Refused(f"{tid} has used {task['rounds']} of {max_rounds} rounds; "
                              "escalate, take it over, or use --override-cap \"<reason>\"")
            task["round_cap_override"] = nxt
            room.event("cap_override", tid, nxt, reason=override_reason)
        rdir = room.round_dir(tid, nxt)
        rdir.mkdir(parents=True, exist_ok=True)
        feedback = rdir / "feedback.md"
        if not feedback.exists():
            store.atomic_write_text(feedback, store.read_text(FEEDBACK_TEMPLATE))
        note = f"round {nxt} prepared" + (f" (cap override: {override_reason})" if override_reason else "")
        transition(task, "REWORK_READY", note)
        room.save_task(task)
        room.event("rework_ready", tid, nxt)
    return feedback


def _verify_cmds(room: Room, tid: str) -> list[str]:
    meta, _ = room.task_meta(tid)
    return meta["verify"] if "verify" in meta else list(room.config["verify"]["default_commands"])


def passing_verification(room: Room, task: dict, head: str) -> str | None:
    """Where a passing verification of exactly `head` (on a clean tree) is recorded, or None."""
    rdir = room.round_dir(task["id"], task["rounds"])
    manual = sorted(rdir.glob("verify-manual-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    for path in reversed(manual):
        rec = store.read_json(path, default={}) or {}
        if rec.get("head") == head and rec.get("tree_clean") and rec.get("status") == "PASSED":
            return path.name
    run = store.read_json(rdir / "run.json", default={}) or {}
    v = run.get("verify") or {}
    if v.get("head") == head and v.get("status") == "PASSED":
        return f"run.json ({v.get('source')})"
    return None


def accept(room: Room, tid: str, message: str, skip_gate_reason: str | None = None) -> list[str]:
    if not message.strip():
        raise UsageError("accept needs -m \"<commit message>\"")
    with locked(room, f"accept {tid}"):
        task = room.load_task(tid)
        if task["status"] != "AWAITING_REVIEW":
            raise Refused(f"{tid} is {task['status']}; accept needs AWAITING_REVIEW")
        repo, base, branch = room.repo, room.config["project"]["base_branch"], task.get("branch")
        if not branch or not task.get("base_sha"):
            raise Refused(f"{tid} has no task branch")
        current = gitops.current_branch(repo)
        if current != branch:
            raise Refused(f"HEAD must be on {branch} to accept (it is on {current or 'a detached HEAD'})")
        gitops.settle_stat_dirty(repo)
        dirty = gitops.dirty_entries(repo)
        if dirty:
            raise Refused("working tree is not clean: " + "; ".join(dirty[:10]))
        if gitops.rev(repo, base) != task["base_sha"]:
            raise Refused(f"{base} moved since {tid} started (it was {task['base_sha'][:7]}); integrate by hand")
        if gitops.has_diff(repo, task["base_sha"], branch):
            if _verify_cmds(room, tid):
                evidence = passing_verification(room, task, gitops.rev(repo, branch))
                if evidence is None and not skip_gate_reason:
                    raise Refused(f"no passing verification of the current {branch} head. Review the diff, "
                                  f"run `verify {tid}`, then accept (or --skip-verify-gate \"<reason>\")")
                if evidence is None:
                    room.event("verify_gate_skipped", tid, task["rounds"], reason=skip_gate_reason)
            gitops.switch(repo, base)
            result = gitops.squash_merge(repo, branch, message)
            if not result["ok"]:
                gitops.switch(repo, branch)
                raise Refused(f"the squash commit failed (a hook?). Nothing changed; HEAD is back on {branch}.\n"
                              + result["output"].strip())
            commit, note = result["sha"], f"squash commit {result['sha'][:7]}"
        else:
            gitops.switch(repo, base)
            commit, note = None, "no changes to merge"
        task["accepted_commit"] = commit
        transition(task, "ACCEPTED", note)
        room.save_task(task)
        st = room.load_state()
        st["open_task"] = None
        room.save_state(st)
        room.event("task_accepted", tid, task["rounds"], commit=commit)
    return [f"{tid} accepted: {note} on {base}; branch {branch} kept. Nothing was pushed."]


def reject(room: Room, tid: str, reason: str, stash: bool) -> list[str]:
    with locked(room, f"reject {tid}"):
        task = room.load_task(tid)
        if task["status"] not in ("AWAITING_REVIEW", "REWORK_READY"):
            raise Refused(f"{tid} is {task['status']}; reject needs AWAITING_REVIEW or REWORK_READY")
        repo, base = room.repo, room.config["project"]["base_branch"]
        lines = []
        gitops.settle_stat_dirty(repo)
        dirty = gitops.dirty_entries(repo)
        if dirty:
            if not stash:
                raise Refused("working tree is not clean (" + "; ".join(dirty[:10]) + "); "
                              "use --stash-leftovers to keep the leftovers in a git stash")
            gitops.stash_leftovers(repo, f"foreman {tid} leftovers")
            lines.append("leftovers saved with git stash")
        if gitops.current_branch(repo) != base:
            gitops.switch(repo, base)
        transition(task, "REJECTED", reason)
        room.save_task(task)
        st = room.load_state()
        st["open_task"] = None
        room.save_state(st)
        room.event("task_rejected", tid, task["rounds"], reason=reason)
    lines.append(f"{tid} rejected; branch {task.get('branch') or '-'} kept; HEAD on {base}.")
    return lines


def stop(room: Room, reason: str) -> list[str]:
    running = room.load_state().get("running")
    if not running:
        raise Refused("no round is running")
    store.atomic_write_text(room.dir / STOP_MARKER, reason or "stop requested")
    room.event("stop_requested", running["task"], running["round"], reason=reason)
    lines = []
    pid, image = running.get("agent_pid"), running.get("agent_image")
    if pid and proc.is_alive(pid, image):
        proc.kill_tree(pid)
        for _ in range(50):
            if not proc.is_alive(pid, image):
                break
            time.sleep(0.1)
        lines.append(f"killed the agent process tree (pid {pid})")
    else:
        lines.append("the agent was not running")
    if proc.is_alive(running.get("dispatcher_pid"), running.get("dispatcher_image")):
        lines.append("the dispatcher is recording the round as INTERRUPTED (partial work gets a snapshot)")
        return lines
    with locked(room, "stop (recovery)"):
        if room.load_state().get("running"):
            recover(room, None)
    lines.append("the dispatcher was gone; the round was recovered as INTERRUPTED")
    return lines


def pause(room: Room, reason: str) -> list[str]:
    store.atomic_write_text(room.dir / "PAUSE", reason)
    room.event("paused", reason=reason)
    return ["room paused: no new dispatch until `unpause` (a running round is not affected; use `stop`)"]


def unpause(room: Room) -> list[str]:
    path = room.dir / "PAUSE"
    if not path.exists():
        return ["room was not paused"]
    path.unlink()
    room.event("unpaused")
    return ["room unpaused"]


def manual_verify(room: Room, tid: str) -> list[str]:
    with locked(room, f"verify {tid}"):
        task = room.load_task(tid)
        n = task["rounds"]
        if n == 0:
            raise Refused(f"{tid} has not been dispatched yet")
        cmds = _verify_cmds(room, tid)
        if not cmds:
            raise Refused(f"{tid} has no verify commands")
        rdir = room.round_dir(tid, n)
        k = 1 + len(list(rdir.glob("verify-manual-*.json")))
        gitops.settle_stat_dirty(room.repo)
        head, clean = gitops.rev(room.repo), not gitops.dirty_entries(room.repo)
        results = verify.run_room(cmds, room.repo, room.config["verify"]["timeout_minutes"], rdir,
                                  prefix=f"verify-manual-{k}")
        ok = all(r["room_exit_code"] == 0 and not r["timed_out"] for r in results)
        status = "PASSED" if ok else "FAILED"
        store.write_json(rdir / f"verify-manual-{k}.json", {
            "ts": store.now_iso(), "head": head, "tree_clean": clean, "status": status, "commands": results})
        room.event("manual_verify", tid, n, status=status, head=head, tree_clean=clean)
    lines = [f"{tid} manual verify #{k}: {status}" + ("" if clean else " (tree was dirty: does not count for accept)")]
    lines += [f"  [{'ok' if r['room_exit_code'] == 0 and not r['timed_out'] else 'FAIL'}] {r['command']}"
              f" (exit {r['room_exit_code']}, {r['duration_s']}s, log {r['log']})" for r in results]
    if ok and clean:
        lines.append(f"next: accept {tid} -m \"<message>\"")
    return lines


def recover(room: Room, holder: dict | None) -> None:
    """Called when the lock holder is gone. Records a running round as INTERRUPTED and snapshots it."""
    st = room.load_state()
    running = st.get("running")
    if not running:
        room.event("stale_lock_removed", holder=holder)
        return
    if proc.is_alive(running.get("agent_pid"), running.get("agent_image")):
        raise Refused(f"the dispatcher is gone but its agent (pid {running['agent_pid']}) is still running; "
                      "run `stop` first")
    tid, n = running["task"], running["round"]
    task = room.load_task(tid)
    try:
        meta, _ = room.task_meta(tid)
    except Exception:  # an unreadable task.md must not block recovery
        meta = {"title": task.get("title", tid)}
    rdir = room.round_dir(tid, n)
    rdir.mkdir(parents=True, exist_ok=True)
    head_before = running.get("head_before") or gitops.rev(room.repo)
    git_part = rounds.capture(room, task, n, head_before, meta.get("allowed_paths") or [],
                              meta.get("title", tid), "INTERRUPTED")
    activity, conversation = {}, None
    stream = rdir / "agy-stdout.jsonl"
    if stream.exists():
        parsed = agent.parse_stream(stream.read_bytes().decode("utf-8", "replace"), room.repo)
        conversation = parsed["conversation_id"]
        activity = {"commands": parsed["commands"], "files_written": parsed["files_written"],
                    "refused": parsed["refused"], "aborted_on": parsed["aborted_on"]}
    run = {
        "schema": 1, "task_id": tid, "round": n, "room_version": __version__, "title": meta.get("title"),
        "base_branch": room.config["project"]["base_branch"], "run_status": "INTERRUPTED",
        "recovered": True, "recovered_at": store.now_iso(), "lock_holder": holder,
        "result_status": "MISSING", "agent_status": None,
        "agent": {"exit_code": None, "conversation_id": conversation},
        "activity": activity, "git": git_part["git"], "checks": git_part["checks"],
        "verify": {"status": "NOT_RUN", "source": None, "commands": []},
        "warnings": git_part["warnings"] + ["RECOVERED"], "notices": [], "verdict_hint": "NEEDS_ATTENTION",
    }
    store.write_json(rdir / "run.json", run)
    if task["status"] == "RUNNING":
        transition(task, "AWAITING_REVIEW", f"round {n}: recovered after the dispatcher died")
        room.save_task(task)
    st = room.load_state()
    st["running"] = None
    room.save_state(st)
    with contextlib.suppress(FileNotFoundError):
        (room.dir / STOP_MARKER).unlink()
    room.event("recovered_interrupted", tid, n, holder=holder)
