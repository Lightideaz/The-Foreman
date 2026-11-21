"""Stack detection for the Add-a-project wizard: read marker files, suggest settings, never run anything."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

CATALOGUE = Path(__file__).with_name("stacks.toml")
BASE_SETTINGS = Path(__file__).resolve().parent.parent / "templates" / "agy-settings.base.json"
MAX_READ = 512 * 1024


def load_catalogue(path: Path = CATALOGUE) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def base_deny_patterns(settings_path: Path = BASE_SETTINGS) -> list[str]:
    """The regex part of every `command(regex:...)` deny rule in the base settings template."""
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    rules = (data.get("permissions") or {}).get("deny") or []
    return [r[len("command(regex:"):-1] for r in rules if r.startswith("command(regex:") and r.endswith(")")]


def already_denied(probe: str, patterns: list[str]) -> bool:
    """agy matches regex rules against the full command (DESIGN F4), like re.fullmatch."""
    for pattern in patterns:
        try:
            if re.fullmatch(pattern, probe):
                return True
        except re.error:
            continue
    return False


def deny_rule(regex: str) -> str:
    return f"command(regex:{regex})"


def _read(path: Path) -> str:
    try:
        if path.is_file() and path.stat().st_size <= MAX_READ:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _has(repo: Path, marker: str) -> list[str]:
    if any(ch in marker for ch in "*?["):
        return sorted(p.name for p in repo.glob(marker) if p.is_file())
    return [marker] if (repo / marker).exists() else []


def _package_scripts(repo: Path) -> dict:
    try:
        data = json.loads(_read(repo / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _node_manager(repo: Path, managers: list[dict]) -> dict:
    for manager in managers:
        if any((repo / lock).exists() for lock in manager["lockfiles"]):
            return manager
    return managers[-1]


def _pytest_configured(repo: Path) -> bool:
    if (repo / "pytest.ini").exists() or (repo / "conftest.py").exists():
        return True
    checks = (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]"))
    if any(marker in _read(repo / name) for name, marker in checks):
        return True
    return any(re.search(r"(?im)^\s*pytest\b", _read(p)) for p in repo.glob("requirements*.txt"))


class _Suggestions:
    def __init__(self, patterns: list[str]):
        self.patterns = patterns
        self.stacks: list[dict] = []
        self.verify: list[dict] = []
        self.deny: list[dict] = []
        self.protect: list[dict] = []

    def command(self, command: str, reason: str, checked: bool = True) -> None:
        if all(v["command"] != command for v in self.verify):
            self.verify.append({"command": command, "reason": reason, "checked": checked})

    def deny_rule(self, regex: str, probe: str, reason: str) -> None:
        rule = deny_rule(regex)
        if not already_denied(probe, self.patterns) and all(d["rule"] != rule for d in self.deny):
            self.deny.append({"rule": rule, "reason": reason, "checked": True})


def _node(repo: Path, cat: dict, out: _Suggestions) -> None:
    manager = _node_manager(repo, cat["node_manager"])
    scripts = _package_scripts(repo)
    out.stacks.append({"id": "node", "label": f"Node.js ({manager['name']})", "evidence": ["package.json"]})
    for name in cat["node_scripts"] + cat["node_optional_scripts"]:
        if name in scripts:
            cmd = manager["test"] if name == "test" else manager["run"].format(script=name)
            out.command(cmd, f"package.json script `{name}`", name in cat["node_scripts"])
    out.deny_rule(manager["deny"], manager["probe"], f"{manager['name']} installs packages")


def _python(repo: Path, out: _Suggestions) -> None:
    if _pytest_configured(repo):
        out.command("python -m pytest", "pytest is configured")
    elif (repo / "tests").is_dir():
        out.command("python -m unittest discover -s tests", "tests/ folder, no pytest configuration")
    else:
        out.command("python -m unittest discover", "no pytest configuration")


def _composer(repo: Path, out: _Suggestions) -> None:
    try:
        data = json.loads(_read(repo / "composer.json") or "{}")
    except json.JSONDecodeError:
        data = {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if isinstance(scripts, dict) and "test" in scripts:
        out.command("composer test", "composer.json script `test`")


def _protect(repo: Path, cat: dict, out: _Suggestions) -> None:
    for path in cat["ci"]:
        if (repo / path.rstrip("/")).exists():
            out.protect.append({"path": path, "reason": "CI configuration", "checked": False})
    for path in cat["lockfiles"]:
        if (repo / path).is_file():
            out.protect.append({"path": path, "reason": "lockfile", "checked": False})
    skip, names = set(cat["skip_dirs"]), set(cat["dir_names"])

    def walk(folder: Path, depth: int) -> None:
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir() and p.name not in skip)
        except OSError:
            return
        for child in children:
            if child.name in names:
                rel = child.relative_to(repo).as_posix() + "/"
                out.protect.append({"path": rel, "reason": "database migrations", "checked": False})
            elif depth < 3:
                walk(child, depth + 1)

    walk(repo, 1)


def detect(repo: Path, catalogue: dict | None = None, patterns: list[str] | None = None) -> dict:
    """Suggestions for one repository root: stacks found, verify commands, deny rules, protected paths."""
    cat = catalogue or load_catalogue()
    out = _Suggestions(base_deny_patterns() if patterns is None else patterns)
    if (repo / "package.json").is_file():
        _node(repo, cat, out)
    for stack in cat["stack"]:
        found = [name for marker in stack["markers"] for name in _has(repo, marker)]
        if not found or any((repo / s).exists() for s in stack.get("skip_if", [])):
            continue
        out.stacks.append({"id": stack["id"], "label": stack["label"], "evidence": found})
        kind = stack.get("kind")
        if kind == "python":
            _python(repo, out)
        elif kind == "composer":
            _composer(repo, out)
        for cmd in stack.get("commands", []):
            out.command(cmd, stack["label"])
        for cmd in stack.get("optional", []):
            out.command(cmd, stack["label"], checked=False)
        for d in stack.get("deny", []):
            out.deny_rule(d["rule"], d["probe"], d["reason"])
    _protect(repo, cat["protect"], out)
    return {"stacks": out.stacks, "verify": out.verify, "deny": out.deny, "protect": out.protect}
