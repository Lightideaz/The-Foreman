"""Verification evidence: what the agent really ran (from the stream) and what room.py runs itself."""
from __future__ import annotations

from pathlib import Path

from . import proc


def run_room(cmds: list[str], repo: Path, timeout_minutes: float, round_dir: Path,
             prefix: str = "verify") -> list[dict]:
    results = []
    for i, cmd in enumerate(cmds, 1):
        log = round_dir / f"{prefix}-{i}.log"
        r = proc.run_logged(cmd, repo, timeout_minutes * 60, log, shell=True)
        results.append({"command": cmd, "room_exit_code": r["exit_code"], "timed_out": r["timed_out"],
                        "duration_s": r["duration_s"], "log": log.name})
    return results


def agent_evidence(cmds: list[str], parsed: dict) -> dict[str, int | None]:
    """Exit code of each verify command's last run after the agent's last file edit (None = no evidence)."""
    after = parsed["last_write_step"]
    evidence: dict[str, int | None] = {}
    for cmd in cmds:
        runs = [c for c in parsed["commands"]
                if c["command"].strip() == cmd.strip() and c["step"] > after and c["state"] == "DONE"]
        evidence[cmd] = runs[-1]["exit_code"] if runs else None
    return evidence


def combine(cmds: list[str], evidence: dict, room_results: list[dict], eligible: bool,
            room_run: str = "after_round") -> dict:
    """Verification status. With room_run "after_review" the room run waits for Claude's diff review
    (status AWAITING_ROOM_RUN); `verify T` then records it and `accept` requires it."""
    if not cmds:
        return {"status": "SKIPPED", "source": None, "commands": []}
    by_room = {r["command"]: r for r in room_results}
    rows = []
    for cmd in cmds:
        row = {"command": cmd, "agent_exit_code": evidence.get(cmd)}
        row.update({k: v for k, v in by_room.get(cmd, {}).items() if k != "command"})
        rows.append(row)
    if not eligible:
        return {"status": "NOT_RUN", "source": None, "commands": rows}
    if room_results:
        ok = all(r.get("room_exit_code") == 0 and not r.get("timed_out") for r in rows)
        return {"status": "PASSED" if ok else "FAILED", "source": "room", "commands": rows}
    if room_run == "after_review":
        return {"status": "AWAITING_ROOM_RUN", "source": None, "commands": rows}
    codes = [r["agent_exit_code"] for r in rows]
    if any(c not in (0, None) for c in codes):
        status = "FAILED"
    elif all(c == 0 for c in codes):
        status = "PASSED"
    else:
        status = "NOT_RUN"
    return {"status": status, "source": "agent-sandbox", "commands": rows}


def claim_mismatches(claims: list[dict], commands: list[dict]) -> list[str]:
    """Commands Gemini says passed but the activity log does not show passing."""
    seen: dict[str, list[dict]] = {}
    for c in commands:
        seen.setdefault(c["command"].strip(), []).append(c)
    out = []
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("outcome") != "passed":
            continue
        cmd = str(claim.get("command", "")).strip().strip("`").strip()
        runs = seen.get(cmd, [])
        if not runs:
            out.append(f"{cmd}: claimed passed, but it is not in the activity log")
        elif runs[-1].get("state") != "DONE" or runs[-1].get("exit_code") != 0:
            out.append(f"{cmd}: claimed passed, activity shows {runs[-1].get('state')} "
                       f"exit {runs[-1].get('exit_code')}")
    return out
