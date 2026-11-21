"""Shared end-of-round work: git facts, checks and the snapshot commit (used by dispatch and recovery)."""
from __future__ import annotations

from . import checks, gitops
from .state import Room


def capture(room: Room, task: dict, round_no: int, head_before: str, allowed: list[str],
            title: str, run_status: str) -> dict:
    cfg, repo = room.config, room.repo
    policy = cfg["policy"]
    warnings: list[str] = []

    head_after = gitops.rev(repo)
    if head_after != head_before:
        warnings.append("AGENT_COMMITTED")
    files = gitops.changed_files(repo, head_before)

    scope = checks.scope_violations(files, allowed)
    if scope is None:
        warnings.append("NO_SCOPE")
    elif scope:
        warnings.append("SCOPE_VIOLATION")
    protected = checks.protected_touched(files, policy["protected_paths"])
    if protected:
        warnings.append("PROTECTED_PATH")
    sensitive = checks.sensitive_files(files, policy["sensitive_patterns"], policy["sensitive_exceptions"])

    suffix = "" if run_status == "COMPLETED" else f" ({run_status.lower()})"
    message = f"room({task['id']}): round {round_no} snapshot{suffix} - {title}"
    if sensitive:
        warnings.append("SENSITIVE_FILES")
        snap = {"outcome": "SKIPPED_SENSITIVE", "sha": None, "output": ""}
    elif gitops.dirty_entries(repo):
        snap = gitops.snapshot(repo, message)
    else:
        snap = {"outcome": "NO_CHANGES", "sha": None, "output": ""}
    if snap["outcome"] == "FAILED_HOOK":
        warnings.append("SNAPSHOT_FAILED")

    head_now = gitops.rev(repo)
    base = task.get("base_sha")
    insertions, deletions = gitops.numstat(repo, base, head_now) if base else (0, 0)
    return {
        "git": {
            "branch": task.get("branch"), "base_sha": base, "head_before": head_before,
            "head_after_agent": head_after, "snapshot": snap["outcome"], "snapshot_sha": snap["sha"],
            "snapshot_output": snap["output"], "files": files,
            "insertions": insertions, "deletions": deletions,
        },
        "checks": {"scope_violations": scope or [], "protected_paths_touched": protected,
                   "sensitive_files": sensitive},
        "warnings": warnings,
    }
