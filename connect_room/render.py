"""Human-readable output: report.md for a round and the short CLI summary."""
from __future__ import annotations


def _fmt_exit(value) -> str:
    return "-" if value is None else str(value)


def report_md(tid: str, round_no: int, res: dict, run: dict) -> str:
    lines = [f"# {tid} - round {round_no}", "",
             f"- Run: {run['run_status']} | result {run['result_status']} | agent {run.get('agent_status') or '-'}",
             f"- Verdict hint: {run['verdict_hint']}",
             f"- Warnings: {', '.join(run['warnings']) or 'none'}", ""]
    data = res.get("data")
    if data:
        lines += ["## Summary (Gemini)", "", str(data.get("summary", "")).strip() or "-", ""]
        if data.get("blocked_reason"):
            lines += ["## Blocked reason", "", str(data["blocked_reason"]).strip(), ""]
        for title, key in (("Questions", "questions"), ("Open issues", "open_issues")):
            items = data.get(key) or []
            if items:
                lines += [f"## {title}", ""] + [f"- {x}" for x in items] + [""]
        if data.get("files_changed"):
            lines += ["## Files changed (claimed)", ""]
            lines += [f"- `{f.get('path')}`: {f.get('change')}" for f in data["files_changed"] if isinstance(f, dict)]
            lines.append("")
        if data.get("commands_run"):
            lines += ["## Commands run (claimed)", ""]
            lines += [f"- `{c.get('command')}`: {c.get('outcome')}" + (f" - {c['note']}" if c.get("note") else "")
                      for c in data["commands_run"] if isinstance(c, dict)]
            lines.append("")
    if res.get("status") != "VALID":
        lines += ["## Result problems", ""] + [f"- {p}" for p in res.get("problems", [])] + [""]
        if res.get("raw"):
            lines += ["## Raw agent answer", "", "```text", res["raw"][:20000], "```", ""]
    lines.append("_Gemini's statements are claims. The evidence is in run.json: activity, git and verify._")
    return "\n".join(lines) + "\n"


def summary(run: dict, round_dir: str) -> list[str]:
    git, checks, verify = run.get("git") or {}, run.get("checks") or {}, run.get("verify") or {}
    activity, agent = run.get("activity") or {}, run.get("agent") or {}
    lines = [
        f"{run['task_id']} round {run['round']}: {run['run_status']} / {run.get('agent_status') or '-'}"
        f" / verify {verify.get('status', '-')} -> {run['verdict_hint']}",
        f"duration {run.get('duration_s', '-')}s | agent exit {_fmt_exit(agent.get('exit_code'))}"
        f" | agy {agent.get('version') or '-'} | conversation {str(agent.get('conversation_id') or '-')[:8]}",
        f"branch {git.get('branch')} | snapshot {git.get('snapshot')} {str(git.get('snapshot_sha') or '')[:7]}"
        f" | {len(git.get('files') or [])} files this round (+{git.get('insertions', 0)} -{git.get('deletions', 0)} task total)",
        f"checks: scope {', '.join(checks.get('scope_violations') or []) or 'ok'}"
        f" | protected {', '.join(checks.get('protected_paths_touched') or []) or 'none'}"
        f" | sensitive {', '.join(checks.get('sensitive_files') or []) or 'none'}",
    ]
    cmds = activity.get("commands") or []
    ok = sum(1 for c in cmds if c.get("state") == "DONE" and c.get("exit_code") == 0)
    lines.append(f"activity: {len(cmds)} commands ({ok} ok) | {len(activity.get('files_written') or [])} files written"
                 f" | refused by rule {sum(1 for r in activity.get('refused') or [] if r.get('kind') == 'rule')}")
    if activity.get("aborted_on"):
        a = activity["aborted_on"]
        lines.append(f"ABORTED on {a.get('tool')}: {a.get('target')}")
    for c in verify.get("commands") or []:
        code = c["room_exit_code"] if "room_exit_code" in c else c.get("agent_exit_code")
        tag = "not run" if code is None else ("ok" if code == 0 and not c.get("timed_out") else "FAIL")
        lines.append(f"verify [{tag}] {c['command']} (agent {_fmt_exit(c.get('agent_exit_code'))},"
                     f" room {_fmt_exit(c.get('room_exit_code'))})")
    awaiting = verify.get("status") == "AWAITING_ROOM_RUN"
    if awaiting:
        lines.append(f"verify: not run by The Foreman yet - review the diff first, then run `verify {run['task_id']}`")
    lines.append(f"warnings: {', '.join(run.get('warnings') or []) or 'none'}"
                 f" | notices: {', '.join(run.get('notices') or []) or 'none'}")
    if run.get("claim_mismatches"):
        lines += [f"claim mismatch: {m}" for m in run["claim_mismatches"][:3]]
    data = run.get("result_summary") or ""
    if data:
        lines.append("gemini: " + " ".join(str(data).split())[:220])
    lines.append(f"open issues: {run.get('open_issues_count', 0)} | questions: {run.get('questions_count', 0)}")
    lines.append(f"details: {round_dir}")
    if git.get("branch"):
        step = f", run `verify {run['task_id']}`" if awaiting else ""
        lines.append(f"next: review `git diff {run.get('base_branch', 'main')}...{git['branch']}`{step},"
                     " then accept / rework / reject")
    return lines[:30]
