"""room.toml and task.md loading with strict validation (unknown keys are errors)."""
from __future__ import annotations

import copy
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError

_NUM = "number"
_REQUIRED = object()

# sandbox: no command allow rules + toolPermission proceed-in-sandbox, so every command stays in the
#          sandbox and a bypass attempt ends the round (F17). allowlist: exact allow rules (bypassable).
PERMISSION_MODELS = ("sandbox", "allowlist")
# When room.py runs the verify commands itself (outside any sandbox).
ROOM_RUN_MODES = ("after_review", "after_round", "never")

DEFAULT_ENV_SCRUB = ["*TOKEN*", "*SECRET*", "*PASSWORD*", "*API_KEY*", "*ACCESS_KEY*", "GITHUB_*",
                     "GH_*", "ANTHROPIC_*", "OPENAI_*", "AWS_*", "AZURE_*", "NPM_TOKEN"]
DEFAULT_SENSITIVE = [".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk", "*.keystore",
                     "*.jks", "id_rsa*", "id_ed25519*", "credentials.json"]

ROOM_SCHEMA: dict[str, dict[str, tuple[Any, Any]]] = {
    "project": {
        "name": (str, _REQUIRED),
        "repo": (str, _REQUIRED),
        "base_branch": (str, "main"),
        "branch_prefix": (str, "room/"),
    },
    "agent": {
        "command": (str, _REQUIRED),
        "args_prefix": (list, []),
        "tested_version": (str, ""),
        "permission_model": (str, "sandbox"),
        "mode": (str, "accept-edits"),
        "model": (str, ""),
        "effort": (str, ""),
        "sandbox": (bool, True),
        "settings_file": (str, "agy-settings.base.json"),
        "timeout_minutes": (_NUM, 45),
        "max_prompt_chars": (int, 24000),
        "env_scrub": (list, DEFAULT_ENV_SCRUB),
        "env_passthrough": (list, []),
    },
    "verify": {
        "default_commands": (list, []),
        "timeout_minutes": (_NUM, 15),
        "room_run": (str, "after_review"),
    },
    "policy": {
        "max_rounds": (int, 3),
        "protected_paths": (list, []),
        "sensitive_patterns": (list, DEFAULT_SENSITIVE),
        "sensitive_exceptions": (list, [".env.example"]),
    },
}

TASK_SCHEMA = {
    "title": str,
    "allowed_paths": list,
    "verify": list,
    "timeout_minutes": _NUM,
    "extra_allow": list,
    "max_rounds": int,
}

_RULE = re.compile(r"^[a-z_]+\(.+\)$", re.S)


def _type_ok(value: Any, expected: Any) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is list:
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == _NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _type_name(expected: Any) -> str:
    return {bool: "true/false", list: "a list of strings", int: "an integer", str: "a string",
            _NUM: "a number"}.get(expected, str(expected))


def load_room_config(path: Path) -> dict:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"missing {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
    unknown = sorted(set(raw) - set(ROOM_SCHEMA))
    if unknown:
        raise ConfigError(f"{path.name}: unknown section(s): {', '.join(unknown)}")
    out: dict[str, dict] = {}
    for section, fields in ROOM_SCHEMA.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            raise ConfigError(f"{path.name}: [{section}] must be a table")
        extra = sorted(set(given) - set(fields))
        if extra:
            raise ConfigError(f"{path.name}: unknown key(s) in [{section}]: {', '.join(extra)}")
        out[section] = {}
        for key, (expected, default) in fields.items():
            if key in given:
                if not _type_ok(given[key], expected):
                    raise ConfigError(f"{path.name}: {section}.{key} must be {_type_name(expected)}")
                out[section][key] = given[key]
            elif default is _REQUIRED:
                raise ConfigError(f"{path.name}: {section}.{key} is required")
            else:
                out[section][key] = copy.deepcopy(default)
    _check_values(out, path.name)
    return out


def _check_values(cfg: dict, where: str) -> None:
    if cfg["agent"]["mode"] != "accept-edits":
        raise ConfigError(f"{where}: agent.mode must be \"accept-edits\" (auto/default are unsafe here)")
    if cfg["agent"]["permission_model"] not in PERMISSION_MODELS:
        raise ConfigError(f"{where}: agent.permission_model must be one of {', '.join(PERMISSION_MODELS)}")
    if cfg["agent"]["permission_model"] == "sandbox" and not cfg["agent"]["sandbox"]:
        raise ConfigError(f"{where}: permission_model \"sandbox\" needs agent.sandbox = true")
    if cfg["verify"]["room_run"] not in ROOM_RUN_MODES:
        raise ConfigError(f"{where}: verify.room_run must be one of {', '.join(ROOM_RUN_MODES)}")
    for key in ("name", "repo", "base_branch", "branch_prefix"):
        if not cfg["project"][key].strip():
            raise ConfigError(f"{where}: project.{key} must not be empty")
    if not cfg["agent"]["command"].strip():
        raise ConfigError(f"{where}: agent.command must not be empty")
    for section, key in (("agent", "timeout_minutes"), ("verify", "timeout_minutes"),
                         ("agent", "max_prompt_chars"), ("policy", "max_rounds")):
        if cfg[section][key] <= 0:
            raise ConfigError(f"{where}: {section}.{key} must be greater than zero")


def parse_task(text: str) -> tuple[dict, str]:
    """Split task.md into (front matter, body). Front matter is TOML between two +++ lines."""
    lines = text.lstrip("﻿").splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        raise ConfigError("task.md must start with a line containing only +++")
    for i in range(1, len(lines)):
        if lines[i].strip() == "+++":
            front, body = "".join(lines[1:i]), "".join(lines[i + 1:])
            break
    else:
        raise ConfigError("task.md: closing +++ line not found")
    try:
        meta = tomllib.loads(front)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"task.md front matter: {exc}") from None
    unknown = sorted(set(meta) - set(TASK_SCHEMA))
    if unknown:
        raise ConfigError(f"task.md: unknown key(s): {', '.join(unknown)}")
    for key, expected in TASK_SCHEMA.items():
        if key in meta and not _type_ok(meta[key], expected):
            raise ConfigError(f"task.md: {key} must be {_type_name(expected)}")
    if not str(meta.get("title", "")).strip():
        raise ConfigError("task.md: title is required")
    for key in ("timeout_minutes", "max_rounds"):
        if key in meta and meta[key] <= 0:
            raise ConfigError(f"task.md: {key} must be greater than zero")
    for rule in meta.get("extra_allow", []):
        if not _RULE.match(rule):
            raise ConfigError(f"task.md: extra_allow entry {rule!r} is not a rule like command(...)")
    if "allowed_paths" in meta:
        meta["allowed_paths"] = [_norm_path(p) for p in meta["allowed_paths"] if p.strip()]
    if "verify" in meta:
        meta["verify"] = [c.strip() for c in meta["verify"] if c.strip()]
    return meta, body.lstrip("\r\n")


def _norm_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def toml_str(value: str) -> str:
    """A TOML string literal; single-quoted (no escapes) when possible, so Windows paths stay readable."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)
