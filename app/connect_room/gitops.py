"""Every git read and write The Foreman performs. The agent never writes to git; this module does."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import proc
from .errors import RoomError

SNAPSHOT_IDENTITY = ["-c", "user.name=The Foreman", "-c", "user.email=foreman@localhost"]
_BASE = ["-c", "core.quotepath=false"]


class GitError(RoomError):
    pass


def run(repo: Path, *args: str, timeout: float = 120, check: bool = True,
        extra: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", *_BASE, *(extra or []), "-C", str(repo), *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         env=env, **proc.popen_kwargs())
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill_tree(p.pid)
        p.communicate()
        raise GitError(f"git {' '.join(args)} timed out after {timeout:g}s") from None
    res = subprocess.CompletedProcess(cmd, p.returncode, out.decode("utf-8", "replace"),
                                      err.decode("utf-8", "replace"))
    if check and res.returncode != 0:
        detail = (res.stderr.strip() or res.stdout.strip())[:2000]
        raise GitError(f"git {' '.join(args)} failed ({res.returncode}): {detail}")
    return res


def is_repo(repo: Path) -> bool:
    r = run(repo, "rev-parse", "--is-inside-work-tree", check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def rev(repo: Path, ref: str = "HEAD") -> str:
    return run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def current_branch(repo: Path) -> str:
    return run(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()


def branch_exists(repo: Path, name: str) -> bool:
    return run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0


def dirty_entries(repo: Path) -> list[str]:
    """`git status` entries (tracked and untracked) as 'XY path' strings; empty means clean."""
    parts = run(repo, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout.split("\0")
    entries, i = [], 0
    while i < len(parts):
        item = parts[i]
        if item:
            entries.append(item)
            if item[:1] in ("R", "C"):
                i += 1  # skip the original path of a rename/copy
        i += 1
    return entries


def settle_stat_dirty(repo: Path) -> list[str]:
    """Clear "modified" flags on files whose content equals the index (e.g. after a prettier hook
    rewrote line endings under core.autocrlf). Never stages a real content change."""
    settled = []
    for entry in dirty_entries(repo):
        if entry[:2] != " M":
            continue
        path = entry[3:]
        worktree = run(repo, "hash-object", "--", path, check=False).stdout.strip()
        staged = run(repo, "rev-parse", "--verify", "-q", f":{path}", check=False).stdout.strip()
        if worktree and worktree == staged:
            run(repo, "add", "--", path)
            settled.append(path)
    return settled


def operation_in_progress(repo: Path) -> str | None:
    gitdir = Path(run(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    for marker, name in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                         ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
        if (gitdir / marker).exists():
            return name
    return None


def create_branch(repo: Path, name: str, start: str) -> None:
    run(repo, "switch", "-c", name, start)


def switch(repo: Path, name: str) -> None:
    run(repo, "switch", name)


def changed_files(repo: Path, since: str) -> list[dict]:
    """Changes between commit `since` and the working tree (committed, staged, unstaged, untracked)."""
    parts = run(repo, "diff", "--name-status", "-z", "-M", since).stdout.split("\0")
    files, i = [], 0
    while i < len(parts):
        status = parts[i]
        if not status:
            i += 1
            continue
        code = status[0]
        if code in ("R", "C"):
            files.append({"path": parts[i + 2], "status": code, "old_path": parts[i + 1]})
            i += 3
        else:
            files.append({"path": parts[i + 1], "status": code})
            i += 2
    for path in run(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0"):
        if path:
            files.append({"path": path, "status": "?"})
    return files


def snapshot(repo: Path, message: str, timeout: float = 300) -> dict:
    """Stage everything and commit with the Foreman identity. Hooks run normally."""
    run(repo, "add", "-A")
    if run(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return {"outcome": "NO_CHANGES", "sha": None, "output": ""}
    try:
        r = run(repo, "commit", "-m", message, extra=SNAPSHOT_IDENTITY, timeout=timeout, check=False)
    except GitError as exc:
        return {"outcome": "FAILED_HOOK", "sha": None, "output": str(exc)}
    if r.returncode != 0:
        return {"outcome": "FAILED_HOOK", "sha": None, "output": (r.stdout + r.stderr)[-4000:]}
    settle_stat_dirty(repo)
    return {"outcome": "COMMITTED", "sha": rev(repo), "output": ""}


def numstat(repo: Path, a: str, b: str) -> tuple[int, int]:
    ins = dels = 0
    for line in run(repo, "diff", "--numstat", a, b).stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            ins += int(cols[0]) if cols[0].isdigit() else 0
            dels += int(cols[1]) if cols[1].isdigit() else 0
    return ins, dels


def has_diff(repo: Path, a: str, b: str) -> bool:
    return run(repo, "diff", "--quiet", a, b, check=False).returncode == 1


def diff_stat(repo: Path, a: str, b: str) -> str:
    return run(repo, "diff", "--stat", a, b).stdout.rstrip()


def squash_merge(repo: Path, branch: str, message: str, timeout: float = 300) -> dict:
    """Squash `branch` onto the current branch as one commit (hooks run). On failure the squash is
    undone with `reset --merge`, which only drops the staged squash; the work stays on `branch`."""
    run(repo, "merge", "--squash", branch)
    try:
        r = run(repo, "commit", "-m", message, timeout=timeout, check=False)
        output = r.stdout + r.stderr
        ok = r.returncode == 0
    except GitError as exc:
        output, ok = str(exc), False
    if ok:
        settle_stat_dirty(repo)
        return {"ok": True, "sha": rev(repo), "output": ""}
    run(repo, "reset", "--merge", check=False)
    return {"ok": False, "sha": None, "output": output[-4000:]}


def stash_leftovers(repo: Path, message: str) -> None:
    run(repo, "stash", "push", "-u", "-m", message)
