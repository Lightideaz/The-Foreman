"""Shared test fixtures: temporary git repos, rooms wired to the fake agent, in-process CLI calls."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_HOME = Path(__file__).resolve().parent.parent
if str(TOOL_HOME) not in sys.path:
    sys.path.insert(0, str(TOOL_HOME))

from connect_room import cli, store  # noqa: E402
from connect_room.state import new_room_state  # noqa: E402

FAKE_AGENT = TOOL_HOME / "tests" / "fake_agent.py"
PY = sys.executable
OK_CMD = f'"{PY}" -c "print(1)"'
FAIL_CMD = f'"{PY}" -c "import sys; sys.exit(3)"'


def q(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "greet.js").write_text("module.exports = {};\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text("# Test\n", encoding="utf-8", newline="\n")
    (repo / ".env.example").write_text("KEY=\n", encoding="utf-8", newline="\n")
    git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Test"), ("user.email", "test@localhost"), ("core.autocrlf", "false")):
        git(repo, "config", key, value)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def make_room(root: Path, repo: Path, verify=(OK_CMD,), room_run="after_round", max_rounds=3,
              timeout_minutes=1, permission_model="sandbox") -> Path:
    room = root / "room"
    room.mkdir()
    toml = "\n".join([
        "[project]", 'name = "test"', f"repo = {q(repo)}", 'base_branch = "main"', 'branch_prefix = "room/"', "",
        "[agent]", f"command = {q(PY)}", f"args_prefix = [{q(FAKE_AGENT)}]", 'tested_version = "1.5.9"',
        f"permission_model = {q(permission_model)}",
        'mode = "accept-edits"', "sandbox = true", f"timeout_minutes = {timeout_minutes}", "",
        "[verify]", f"default_commands = [{', '.join(q(c) for c in verify)}]", "timeout_minutes = 1",
        f"room_run = {q(room_run)}", "",
        "[policy]", f"max_rounds = {max_rounds}", 'protected_paths = ["docs/"]', "",
    ])
    (room / "room.toml").write_text(toml, encoding="utf-8")
    shutil.copyfile(TOOL_HOME / "templates" / "agy-settings.base.json", room / "agy-settings.base.json")
    store.write_json(room / "state.json", new_room_state())
    return room


def run_cli(*args) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


def write_task(room: Path, tid: str, allowed=("src/",), verify=None, extra: str = "", title="Test task") -> None:
    lines = ["+++", f"title = {q(title)}", f"allowed_paths = [{', '.join(q(a) for a in allowed)}]"]
    if verify is not None:
        lines.append(f"verify = [{', '.join(q(c) for c in verify)}]")
    if extra:
        lines.append(extra)
    lines += ["+++", "", "## Goal", "Do the thing.", ""]
    (room / "tasks" / tid / "task.md").write_text("\n".join(lines), encoding="utf-8")


@contextlib.contextmanager
def scenario(name: str, **env):
    saved = {k: os.environ.get(k) for k in ["FAKE_AGENT_SCENARIO", *env]}
    os.environ["FAKE_AGENT_SCENARIO"] = name
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def read_run(room: Path, tid: str, n: int = 1) -> dict:
    return json.loads((room / "tasks" / tid / "rounds" / str(n) / "run.json").read_text(encoding="utf-8"))


class RoomCase(unittest.TestCase):
    """A fresh repo and room per test."""
    verify = (OK_CMD,)
    room_run = "after_round"
    permission_model = "sandbox"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.room = make_room(self.root, self.repo, verify=self.verify, room_run=self.room_run,
                              permission_model=self.permission_model)

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args):
        return run_cli("--room", self.room, *args)

    def new_task(self, **task_kwargs) -> str:
        code, out, err = self.cli("new", task_kwargs.pop("title", "Test task"))
        self.assertEqual(code, 0, err)
        tid = out.split()[0]
        write_task(self.room, tid, **task_kwargs)
        return tid

    def dispatch(self, tid: str, name: str, **env) -> tuple[int, str, str]:
        with scenario(name, **env):
            return self.cli("dispatch", tid)
