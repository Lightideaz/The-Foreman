"""Scope, protected-path and sensitive-file checks over the files a round changed.

These detect problems after the fact; they do not prevent them.
"""
from __future__ import annotations

import fnmatch
import posixpath


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _under(path: str, prefix: str) -> bool:
    prefix = _norm(prefix).rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def touched_paths(files: list[dict]) -> list[str]:
    paths = set()
    for f in files:
        paths.add(_norm(f["path"]))
        if f.get("old_path"):
            paths.add(_norm(f["old_path"]))
    return sorted(paths)


def scope_violations(files: list[dict], allowed: list[str]) -> list[str] | None:
    """None when no scope was given; otherwise the touched paths outside every allowed prefix."""
    if not allowed:
        return None
    return [p for p in touched_paths(files) if not any(_under(p, a) for a in allowed)]


def protected_touched(files: list[dict], protected: list[str]) -> list[str]:
    return [p for p in touched_paths(files) if any(_under(p, a) for a in protected)]


def sensitive_files(files: list[dict], patterns: list[str], exceptions: list[str]) -> list[str]:
    hits = []
    for f in files:
        if f["status"] == "D":
            continue
        path = _norm(f["path"])
        name = posixpath.basename(path).lower()
        if any(fnmatch.fnmatchcase(name, e.lower()) for e in exceptions):
            continue
        if any(fnmatch.fnmatchcase(name, p.lower()) for p in patterns):
            hits.append(path)
    return hits
