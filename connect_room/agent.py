"""Everything specific to the agy CLI: permission settings, prompt, argv, environment,
event-stream parsing and result validation (facts F1-F16 in DESIGN.md)."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import ConfigError

STREAM_FORMAT = "stream-json"
WRITE_TOOLS = {"write_file", "edit_file", "write_to_file", "replace_file_content",
               "multi_replace_file_content", "sed_file", "notebook_edit"}
RESULT_STATUSES = ("DONE", "BLOCKED", "FAILED")
OUTCOMES = ("passed", "failed", "denied", "not_run")
REQUIRED_KEYS = ("status", "summary", "files_changed", "commands_run", "questions", "open_issues")

_EXIT_RE = re.compile(r"Command exited with code (-?\d+)")
_RE2_SPECIAL = set("\\.+*?()|[]{}^$")


# ---------------------------------------------------------------- permissions and prompt

def re2_escape(text: str) -> str:
    """Escape only RE2 metacharacters (Python's re.escape also escapes spaces, which RE2 rejects)."""
    return "".join("\\" + c if c in _RE2_SPECIAL else c for c in text)


def exact_rule(command: str) -> str:
    return f"command(regex:^{re2_escape(command.strip())}$)"


def build_settings(base_text: str, repo: Path, verify_cmds: list[str], extra_allow: list[str],
                   model: str = "sandbox") -> dict:
    repo_posix = json.dumps(Path(repo).resolve().as_posix())[1:-1]
    try:
        settings = json.loads(base_text.replace("{REPO}", repo_posix))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"agent settings file is not valid JSON: {exc}") from None
    perms = settings.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ConfigError("agent settings: permissions must be an object")
    allow = perms.setdefault("allow", [])
    perms.setdefault("deny", [])
    if not isinstance(allow, list) or not isinstance(perms["deny"], list):
        raise ConfigError("agent settings: permissions.allow and permissions.deny must be lists")
    if model == "sandbox":
        # Any matching allow rule also approves running that command outside the sandbox (F17),
        # so the sandbox model keeps no command allow rules at all.
        bad = [r for r in extra_allow if r.startswith("command(")]
        if bad:
            raise ConfigError("extra_allow command rules are not allowed in the sandbox model (an allow rule "
                              f"lets the agent run that command outside the sandbox): {', '.join(bad)}")
        perms["allow"] = [r for r in allow if not r.startswith("command(")] + [
            r for r in extra_allow if r not in allow]
        settings["toolPermission"] = "proceed-in-sandbox"
        return settings
    for rule in [exact_rule(c) for c in verify_cmds] + list(extra_allow):
        if rule not in allow:
            allow.append(rule)
    return settings


def _unescape_exact(pattern: str) -> str | None:
    """The literal command behind ^escaped$, or None if the pattern is a real regex."""
    if len(pattern) < 2 or not (pattern.startswith("^") and pattern.endswith("$")):
        return None
    body, out, i = pattern[1:-1], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body) and body[i + 1] in _RE2_SPECIAL:
            out.append(body[i + 1])
            i += 2
            continue
        if c in _RE2_SPECIAL:
            return None
        out.append(c)
        i += 1
    return "".join(out)


def describe_rule(rule: str) -> str:
    m = re.fullmatch(r"command\((.*)\)", rule, re.S)
    if not m:
        return rule
    inner = m.group(1)
    if not inner.startswith("regex:"):
        return inner
    pattern = inner[len("regex:"):]
    literal = _unescape_exact(pattern)
    return literal if literal is not None else f"any command matching the regular expression {pattern}"


SANDBOX_COMMANDS_TEXT = [
    "  - Any command, but only inside the sandbox (no network, no access to the user profile).",
    "  - Commands on the deny list are refused.",
    "  - Never run anything outside the sandbox: any attempt ends your run without a result.",
    "  - If a command fails because of the sandbox (for example EPERM), do not retry it outside;",
    "    report it as not_run with the error and carry on.",
]


def build_prompt(preamble: str, tid: str, round_no: int, branch: str, meta: dict, body: str,
                 verify_cmds: list[str], allow_rules: list[str], feedback: str | None = None,
                 previous: str | None = None, model: str = "sandbox") -> str:
    allowed = meta.get("allowed_paths") or []
    lines = [
        "## Task metadata",
        f"- Task id: {tid}",
        f"- Round: {round_no}",
        f"- Branch: {branch} (The Foreman manages git; never run git write commands)",
        "- Allowed paths: " + (", ".join(allowed) if allowed else "(not restricted; change only what the task needs)"),
        "- Verification commands (run after your last edit):",
    ]
    lines += [f"  - {c}" for c in verify_cmds] if verify_cmds else ["  - (none)"]
    if model == "sandbox":
        lines.append("- Commands you may run (one at a time):")
        lines += SANDBOX_COMMANDS_TEXT
    else:
        lines.append("- Commands you may run (exactly as written, one at a time; anything else ends your run):")
        lines += [f"  - {describe_rule(r)}" for r in allow_rules] if allow_rules else ["  - (none)"]
    parts = [preamble.strip(), "\n".join(lines), "## Task\n\n" + body.strip()]
    if previous:
        parts.append("## Previous round (facts recorded by The Foreman)\n\n" + previous.strip())
    if feedback:
        parts.append("## Feedback from Claude for this round\n\n" + feedback.strip())
    return "\n\n".join(parts) + "\n"


def _fmt_minutes(minutes: float) -> str:
    return str(int(minutes)) if float(minutes).is_integer() else f"{minutes:g}"


def build_argv(agent_cfg: dict, prompt: str, schema_path: Path, settings_path: Path,
               timeout_minutes: float, conversation: str | None = None) -> list[str]:
    argv = [agent_cfg["command"], *agent_cfg["args_prefix"], "-p", prompt,
            "--output-format", STREAM_FORMAT, "--mode", agent_cfg["mode"],
            "--json-schema", str(schema_path), "--settings", str(settings_path),
            "--print-timeout", f"{_fmt_minutes(timeout_minutes)}m"]
    if agent_cfg["sandbox"]:
        argv.append("--sandbox")
    if agent_cfg["model"]:
        argv += ["--model", agent_cfg["model"]]
    if agent_cfg["effort"]:
        argv += ["--effort", agent_cfg["effort"]]
    if conversation:
        argv += ["--conversation", conversation]
    return argv


def agent_env(base: dict[str, str], scrub: list[str], passthrough: list[str]) -> dict[str, str]:
    keep = {p.upper() for p in passthrough}
    patterns = [p.upper() for p in scrub]
    return {k: v for k, v in base.items()
            if k.upper() in keep or not any(fnmatch.fnmatchcase(k.upper(), p) for p in patterns)}


def agent_version(agent_cfg: dict, timeout: float = 60) -> str:
    try:
        r = subprocess.run([agent_cfg["command"], *agent_cfg["args_prefix"], "--version"],
                           capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"agent command failed to report its version: {exc}") from None
    text = r.stdout.decode("utf-8", "replace").strip()
    return text.splitlines()[0].strip() if text else ""


# ---------------------------------------------------------------- stream parsing

def _refusal_kind(message: str) -> str | None:
    if "deny rule" in message:
        return "rule"   # explicit deny: the agent continues (F3)
    if "denied permission" in message:
        return "ask"    # no matching rule: headless auto-deny ends the run (F2)
    return None


def rel_to_repo(path: str, repo: Path) -> str:
    if not path:
        return path
    try:
        p, r = os.path.abspath(path), os.path.abspath(repo)
        if os.path.normcase(p) == os.path.normcase(r) or os.path.normcase(p).startswith(os.path.normcase(r) + os.sep):
            return os.path.relpath(p, r).replace("\\", "/")
    except ValueError:
        pass
    return path.replace("\\", "/")


def parse_stream(text: str, repo: Path) -> dict:
    events, bad_lines = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)

    init, result, conversation = {}, None, None
    steps: dict[int, dict] = {}
    for e in events:
        conversation = conversation or e.get("conversation_id")
        if e.get("event") == "init":
            init = e.get("init") or {}
        elif e.get("event") == "result":
            result = e.get("result") or {}
        upd = e.get("step_update")
        if isinstance(upd, dict) and upd.get("step_type") == "tool" and isinstance(upd.get("step_index"), int):
            steps[upd["step_index"]] = upd  # the last update of a step carries its final state

    commands, written, refused = [], [], []
    last_write_step, last_error = -1, None
    for idx in sorted(steps):
        s = steps[idx]
        info = s.get("tool_info") or {}
        name = s.get("tool_name") or info.get("name") or ""
        params = info.get("parameters") or {}
        state = s.get("state")
        message = str((info.get("error") or {}).get("message") or "")
        kind = _refusal_kind(message)
        if name == "run_command":
            target = str(params.get("CommandLine") or "")
        else:
            target = rel_to_repo(str(params.get("Path") or params.get("TargetFile")
                                     or params.get("AbsolutePath") or ""), repo)
        if state == "ERROR":
            last_error = {"tool": name, "target": target, "message": message[:300]}
        if kind:
            refused.append({"step": idx, "tool": name, "target": target, "kind": kind, "message": message[:300]})
        if name == "run_command":
            entry: dict[str, Any] = {"step": idx, "command": target, "state": state, "exit_code": None}
            if state == "DONE":
                output = str(info.get("output") or "")
                m = _EXIT_RE.search(output)
                if m:
                    entry["exit_code"] = int(m.group(1))
                else:
                    entry["note"] = output[:300]  # DONE without an exit code is a failure (F10)
            elif kind:
                entry["state"] = "DENIED_BY_RULE" if kind == "rule" else "DENIED_ASK"
            else:
                entry["error"] = message[:300]
            commands.append(entry)
        elif name in WRITE_TOOLS and state == "DONE":
            if target not in written:
                written.append(target)
            last_write_step = idx

    structured = (result or {}).get("structured_output")
    denied = (result or {}).get("denied_actions") or []
    aborted_on = None
    if result is not None and not structured and denied:
        asks = [r for r in refused if r["kind"] == "ask"]
        source = asks[-1] if asks else last_error
        aborted_on = ({"tool": source["tool"], "target": source["target"]} if source
                      else {"tool": str(denied[-1].get("action", "?")), "target": "?"})
    return {
        "init": init, "result": result, "conversation_id": conversation, "bad_lines": bad_lines,
        "commands": commands, "files_written": written, "last_write_step": last_write_step,
        "refused": refused, "denied_actions": denied, "aborted_on": aborted_on,
    }


# ---------------------------------------------------------------- result contract

def validate_result(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return ["the result is not a JSON object"]
    problems = [f"unexpected key {k!r}" for k in obj if k not in REQUIRED_KEYS and k != "blocked_reason"]
    problems += [f"missing key {k!r}" for k in REQUIRED_KEYS if k not in obj]
    if obj.get("status") not in RESULT_STATUSES:
        problems.append("status must be DONE, BLOCKED or FAILED")
    if not isinstance(obj.get("summary", ""), str):
        problems.append("summary must be a string")
    for item in obj.get("files_changed", []) if isinstance(obj.get("files_changed"), list) else [None]:
        if not (isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("change"), str)):
            problems.append("files_changed entries need string path and change")
            break
    for item in obj.get("commands_run", []) if isinstance(obj.get("commands_run"), list) else [None]:
        if not (isinstance(item, dict) and isinstance(item.get("command"), str)
                and item.get("outcome") in OUTCOMES and isinstance(item.get("note", ""), str)):
            problems.append("commands_run entries need a command and an outcome of passed/failed/denied/not_run")
            break
    for key in ("questions", "open_issues"):
        value = obj.get(key, [])
        if not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
            problems.append(f"{key} must be a list of strings")
    if obj.get("status") == "BLOCKED" and not str(obj.get("blocked_reason", "")).strip():
        problems.append("status BLOCKED needs blocked_reason")
    return problems


def extract_result(parsed: dict) -> dict:
    """Status VALID / INVALID / MISSING plus the data, raw text and validation problems."""
    result = parsed["result"]
    if result is None:
        return {"status": "MISSING", "data": None, "raw": "", "problems": ["no result event in the stream"]}
    obj = result.get("structured_output")
    raw = str(result.get("response") or "")
    if obj is None and raw.strip():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "INVALID", "data": None, "raw": raw, "problems": ["the answer is not JSON"]}
    if obj is None:
        return {"status": "MISSING", "data": None, "raw": raw, "problems": ["no structured output"]}
    problems = validate_result(obj)
    return {"status": "INVALID" if problems else "VALID", "data": obj if isinstance(obj, dict) else None,
            "raw": raw, "problems": problems}
