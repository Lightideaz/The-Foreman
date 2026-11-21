"""Emulates the agy CLI for tests: same flags, the NDJSON stream shape recorded in Phase 0 (F11),
and a behaviour chosen by the FAKE_AGENT_SCENARIO environment variable."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HEARTBEAT = ("import sys, time\n"
             "p = sys.argv[1]\n"
             "while True:\n"
             "    open(p, 'a').write('.')\n"
             "    time.sleep(0.2)\n")


class Emitter:
    def __init__(self):
        self.index = 0
        self.conversation = "fake-conv-0001"

    def emit(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def init(self):
        self.emit({"event": "init", "conversation_id": self.conversation,
                   "init": {"cwd": os.getcwd(), "permission_mode": "request-review",
                            "tools": ["run_command", "write_file", "read_file"]}})

    def step(self, name, params, state="DONE", output=None, error=None):
        idx, self.index = self.index, self.index + 1
        base = {"conversation_id": self.conversation, "step_index": idx, "step_type": "tool", "tool_name": name}
        self.emit({"event": "step_update", "step_update": {**base, "state": "ACTIVE",
                                                           "tool_info": {"name": name, "parameters": params}}})
        info = {"name": name, "parameters": params}
        if output is not None:
            info["output"] = output
        if error is not None:
            info["error"] = {"type": "TOOL_ERROR", "message": error}
        self.emit({"event": "step_update", "step_update": {**base, "state": state, "tool_info": info}})

    def write(self, rel, text):
        path = Path.cwd() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        self.step("write_file", {"Path": str(path)})

    def command(self, cmd, code=0, out="ok"):
        self.step("run_command", {"CommandLine": cmd},
                  output=f"Created At: now\nCommand exited with code {code}.\nCommand output: {out}")

    def deny_rule(self, cmd):
        self.step("run_command", {"CommandLine": cmd}, state="ERROR",
                  error=f'permission check failed for command "{cmd}": Permission denied for command({cmd}). '
                        "Matches user-configured deny rule.")

    def deny_ask(self, cmd):
        self.step("run_command", {"CommandLine": cmd}, state="ERROR",
                  error=f'permission check failed for unsandboxed "{cmd}": user denied permission to run '
                        f"command:\n{cmd}\nDo not attempt to circumvent this denial.")

    def result(self, structured=None, response=None, denied=None, status="SUCCESS"):
        r = {"conversation_id": self.conversation, "status": status,
             "response": response if response is not None else
             (json.dumps(structured, ensure_ascii=False) if structured is not None else ""),
             "duration_seconds": 0.1, "num_turns": 1,
             "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
        if structured is not None:
            r["structured_output"] = structured
        if denied:
            r["denied_actions"] = denied
        self.emit({"event": "result", "result": r})


def answer(status="DONE", summary="did the task", files=None, commands=None, **extra):
    return {"status": status, "summary": summary, "files_changed": files or [], "commands_run": commands or [],
            "questions": [], "open_issues": [], **extra}


def verify_commands(prompt):
    cmds, inside = [], False
    for line in prompt.splitlines():
        if line.startswith("- Verification commands"):
            inside = True
            continue
        if inside:
            if line.startswith("  - "):
                if line[4:].strip() != "(none)":
                    cmds.append(line[4:].strip())
            else:
                break
    return cmds


def run_verify(em, cmds, code=0):
    for c in cmds:
        em.command(c, code)
    return [{"command": c, "outcome": "passed"} for c in cmds]


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if "--version" in argv:
        print("1.5.9")
        return 0
    prompt = argv[argv.index("-p") + 1]
    state_dir = os.environ.get("FAKE_AGENT_STATE_DIR")
    if state_dir:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        Path(state_dir, "argv.json").write_text(json.dumps({
            "argv": argv[:argv.index("-p") + 1] + ["<prompt>"] + argv[argv.index("-p") + 2:],
            "env_has_secret": "CR_TEST_SECRET_TOKEN" in os.environ,
            "env_has_keep": "CR_TEST_KEEP" in os.environ,
            "settings": json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8")),
            "prompt": prompt}, ensure_ascii=False), encoding="utf-8")
    scenario = os.environ.get("FAKE_AGENT_SCENARIO", "done_ok")
    cmds = verify_commands(prompt)
    em = Emitter()

    if scenario == "no_output":
        return 0
    if scenario == "crash":
        sys.stderr.write("boom\n")
        return 1
    if scenario == "bad_settings":
        sys.stderr.write("Error: --settings must be a JSON object of settings.json keys: invalid escape\n")
        return 1

    em.init()
    if scenario in ("done_ok", "done_tests_fail"):
        em.write("src/greet.js", "// farewell added\n")
        em.result(answer(files=[{"path": "src/greet.js", "change": "added a line"}], commands=run_verify(em, cmds)))
    elif scenario == "blocked":
        em.result(answer(status="BLOCKED", summary="cannot decide", blocked_reason="needs a product decision",
                         commands=[]))
    elif scenario == "invalid_json":
        em.result(response="I did it, trust me.")
    elif scenario == "aborted_on_ask":
        em.write("src/greet.js", "// partial\n")
        em.deny_ask("rm -rf build")
        sys.stderr.write("jetski: no output produced - a tool required the \"command\" permission\n")
        em.result(denied=[{"action": "command", "display_name": "Bash"}])
    elif scenario == "deny_rule_continue":
        em.deny_rule("git commit -m x")
        em.write("src/greet.js", "// done after refusal\n")
        em.result(answer(commands=run_verify(em, cmds)))
    elif scenario == "done_but_cmd_failed":
        em.write("src/greet.js", "// changed\n")
        run_verify(em, cmds, code=1)
        em.result(answer(commands=[{"command": c, "outcome": "passed"} for c in cmds]))
    elif scenario == "hang":
        em.write("src/greet.js", "// partial before hang\n")
        time.sleep(600)
    elif scenario == "commits_itself":
        em.write("src/greet.js", "// committed by the agent\n")
        subprocess.run(["git", "-c", "user.name=agent", "-c", "user.email=agent@x", "commit", "-qam", "agent"],
                       check=True)
        em.command("git commit -am agent")
        em.result(answer(commands=run_verify(em, cmds)))
    elif scenario == "out_of_scope":
        em.write("src/greet.js", "// in scope\n")
        em.write("README.md", "out of scope\n")
        em.result(answer(commands=run_verify(em, cmds)))
    elif scenario == "writes_env":
        em.write(".env", "SECRET=1\n")
        em.result(answer(commands=run_verify(em, cmds)))
    elif scenario == "arabic":
        em.write("src/greet.js", "// مع السلامة، سارة\n")
        em.result(answer(summary="أضفت دالة الوداع", commands=run_verify(em, cmds)))
    elif scenario == "spawn_tree":
        em.write("src/partial.txt", "partial work\n")
        pids = [os.getpid()]
        for i in (1, 2):
            child = subprocess.Popen([sys.executable, "-c", HEARTBEAT, str(Path(state_dir, f"hb{i}.txt"))])
            pids.append(child.pid)
        Path(state_dir, "pids.json").write_text(json.dumps(pids), encoding="utf-8")
        while True:
            with open(Path(state_dir, "hb0.txt"), "a") as fh:
                fh.write(".")
            time.sleep(0.2)
    else:
        sys.stderr.write(f"unknown scenario {scenario}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
