"""Phase 1: store, process liveness, lock, config, task front matter, agent helpers, simple commands."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PY, TOOL_HOME, RoomCase, make_repo, run_cli

from connect_room import agent, config, lock, proc, store
from connect_room.errors import ConfigError

ACCEPTANCE_RUNS = TOOL_HOME / "acceptance" / "runs"


class StoreTests(unittest.TestCase):
    def test_atomic_write_retries_transient_permission_errors(self):
        calls = {"n": 0}
        real = store._replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("locked by antivirus")
            real(src, dst)

        with tempfile.TemporaryDirectory() as d:
            store._replace = flaky
            try:
                store.write_json(Path(d) / "x.json", {"ok": "نعم"})
            finally:
                store._replace = real
            self.assertEqual(store.read_json(Path(d) / "x.json"), {"ok": "نعم"})
            self.assertEqual(calls["n"], 3)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["x.json"])

    def test_atomic_write_gives_up_and_leaves_no_temp_file(self):
        real = store._replace

        def always(src, dst):
            raise PermissionError("locked")

        with tempfile.TemporaryDirectory() as d:
            store._replace = always
            try:
                with self.assertRaises(PermissionError):
                    store.atomic_write_text(Path(d) / "x.txt", "data")
            finally:
                store._replace = real
            self.assertEqual(list(Path(d).iterdir()), [])


class ProcTests(unittest.TestCase):
    def test_liveness(self):
        self.assertTrue(proc.is_alive(os.getpid()))
        self.assertTrue(proc.is_alive(os.getpid(), sys.executable))
        self.assertFalse(proc.is_alive(os.getpid(), "notepad.exe"))
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        self.assertFalse(proc.is_alive(child.pid))
        self.assertFalse(proc.is_alive(None))

    def test_kill_tree_kills_grandchildren(self):
        code = ("import subprocess, sys, time; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "print(c.pid, flush=True); time.sleep(60)")
        parent = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE, text=True, **proc.popen_kwargs())
        grandchild = int(parent.stdout.readline())
        self.assertTrue(proc.is_alive(grandchild))
        self.assertTrue(proc.kill_tree(parent.pid))
        parent.communicate(timeout=10)
        for _ in range(50):
            if not proc.is_alive(grandchild):
                break
            import time
            time.sleep(0.1)
        self.assertFalse(proc.is_alive(grandchild))

    def test_package_never_uses_forbidden_calls(self):
        forbidden = ["os.kill", "dangerously-skip-permissions", "--no-verify", '"--hard"', "'--hard'",
                     '"clean"', "'clean'", '"auto"']
        for path in (TOOL_HOME / "connect_room").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in forbidden:
                self.assertNotIn(word, text, f"{word} found in {path.name}")
            # `git stash push` is fine; a git push is not
            self.assertIsNone(re.search(r'(?<!"stash", )["\']push["\']', text), f"git push in {path.name}")

    def test_tool_is_project_neutral(self):
        """Project specifics belong in a room (room.toml, PROJECT-NOTES.md), never in the tool."""
        files = [*(TOOL_HOME / "connect_room").glob("*.py"), *(TOOL_HOME / "templates").iterdir(),
                 *(TOOL_HOME / "prompts").iterdir(), TOOL_HOME / "room.py"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?i)pnpm|turbo\.json|husky|[A-Z]:\\Users\\", text), f"project-specific text in {path.name}")


class LockTests(RoomCase):
    def test_contention_then_stale_recovery(self):
        code = (f"import sys, time; sys.path.insert(0, r'{TOOL_HOME}'); from pathlib import Path; "
                f"from connect_room import lock; lock.acquire(Path(r'{self.room}'), 'test-holder'); "
                "print('locked', flush=True); time.sleep(60)")
        holder = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            rc, _, err = self.cli("new", "blocked")
            self.assertEqual(rc, 3)
            self.assertIn("busy", err)
        finally:
            holder.kill()
            holder.communicate()
        rc, out, err = self.cli("new", "after holder died")
        self.assertEqual(rc, 0, err)
        events = (self.room / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("stale_lock_removed", events)
        self.assertFalse((self.room / lock.LOCK_NAME).exists())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "room.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return config.load_room_config(self.path)

    BASE = "[project]\nname = 'x'\nrepo = 'D:\\Some Path\\repo'\n[agent]\ncommand = 'C:\\x\\agy.exe'\n"

    def test_windows_literal_paths_and_defaults(self):
        cfg = self.load(self.BASE)
        self.assertEqual(cfg["project"]["repo"], "D:\\Some Path\\repo")
        self.assertEqual(cfg["agent"]["mode"], "accept-edits")
        self.assertTrue(cfg["agent"]["sandbox"])
        self.assertIn(".env", cfg["policy"]["sensitive_patterns"])

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self.load(self.BASE + "sandbx = true\n")

    def test_unknown_section_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown section"):
            self.load(self.BASE + "[agnet]\n")

    def test_missing_required_key(self):
        with self.assertRaisesRegex(ConfigError, "repo is required"):
            self.load("[project]\nname = 'x'\n[agent]\ncommand = 'a'\n")

    def test_mode_other_than_accept_edits_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "accept-edits"):
            self.load(self.BASE + "mode = 'auto'\n")

    def test_wrong_type(self):
        with self.assertRaisesRegex(ConfigError, "true/false"):
            self.load(self.BASE + "sandbox = 1\n")

    def test_permission_model_and_room_run(self):
        self.assertEqual(self.load(self.BASE)["agent"]["permission_model"], "sandbox")
        self.assertEqual(self.load(self.BASE)["verify"]["room_run"], "after_review")
        with self.assertRaisesRegex(ConfigError, "permission_model must be"):
            self.load(self.BASE + "permission_model = 'open'\n")
        with self.assertRaisesRegex(ConfigError, "needs agent.sandbox = true"):
            self.load(self.BASE + "sandbox = false\n")
        with self.assertRaisesRegex(ConfigError, "room_run must be"):
            self.load(self.BASE + "[verify]\nroom_run = 'sometimes'\n")


class TaskFrontMatterTests(unittest.TestCase):
    def test_valid_with_arabic_title(self):
        meta, body = config.parse_task("\ufeff+++\ntitle = 'إضافة دالة'\nallowed_paths = ['./src\\a/']\n"
                                       "verify = [' node test.js ']\ntimeout_minutes = 0.5\n+++\n\n## Goal\nx\n")
        self.assertEqual(meta["title"], "إضافة دالة")
        self.assertEqual(meta["allowed_paths"], ["src/a/"])
        self.assertEqual(meta["verify"], ["node test.js"])
        self.assertTrue(body.startswith("## Goal"))

    def test_errors(self):
        cases = {
            "title = 'x'\n": "must start",
            "+++\ntitle = 'x'\n": "closing",
            "+++\ntitle = \n+++\n": "front matter",
            "+++\ntitle = 'x'\ncolour = 1\n+++\n": "unknown key",
            "+++\nallowed_paths = []\n+++\n": "title is required",
            "+++\ntitle = 'x'\nextra_allow = ['node test.js']\n+++\n": "not a rule",
            "+++\ntitle = 'x'\nmax_rounds = 0\n+++\n": "greater than zero",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, message):
                config.parse_task(text)


class AgentHelperTests(unittest.TestCase):
    def test_exact_rule_escapes_only_re2_metacharacters(self):
        rule = agent.exact_rule('npm --workspace @acme/web test -- --grep "a.b(c)"')
        self.assertEqual(rule, 'command(regex:^npm --workspace @acme/web test -- --grep "a\\.b\\(c\\)"$)')
        self.assertEqual(agent.describe_rule(rule), 'npm --workspace @acme/web test -- --grep "a.b(c)"')
        self.assertIn("regular expression", agent.describe_rule("command(regex:^git (status|diff)( .*)?$)"))
        self.assertEqual(agent.describe_rule("command(git status)"), "git status")

    def test_build_settings_fills_repo_and_adds_rules(self):
        base = (TOOL_HOME / "templates" / "agy-settings.base.json").read_text(encoding="utf-8")
        s = agent.build_settings(base, Path("D:/Some Path/repo"), ["node test.js"], ["command(git show)"],
                                 "allowlist")
        deny = s["permissions"]["deny"]
        self.assertTrue(any(r.startswith("write_file(D:/Some Path/repo/.git/") for r in deny))
        self.assertIn("command(regex:^node test\\.js$)", s["permissions"]["allow"])
        self.assertIn("command(git show)", s["permissions"]["allow"])
        self.assertNotIn("toolPermission", s)
        with self.assertRaises(ConfigError):
            agent.build_settings("{not json", Path("."), [], [])

    def test_sandbox_model_settings(self):
        base = (TOOL_HOME / "templates" / "agy-settings.base.json").read_text(encoding="utf-8")
        s = agent.build_settings(base, Path("D:/r"), ["node test.js"], ["read_url(example.com)"], "sandbox")
        self.assertEqual(s["toolPermission"], "proceed-in-sandbox")
        self.assertEqual(s["permissions"]["allow"], ["read_url(example.com)"])
        self.assertTrue(s["permissions"]["deny"])
        with self.assertRaisesRegex(ConfigError, "sandbox model"):
            agent.build_settings(base, Path("D:/r"), [], ["command(git show)"], "sandbox")

    def test_env_scrub(self):
        env = agent.agent_env({"PATH": "p", "GH_TOKEN": "s", "MY_API_KEY": "s", "GEMINI_API_KEY": "k", "HOME": "h"},
                              ["*TOKEN*", "*API_KEY*"], ["GEMINI_API_KEY"])
        self.assertEqual(set(env), {"PATH", "HOME", "GEMINI_API_KEY"})

    def test_validate_result(self):
        good = {"status": "DONE", "summary": "s", "files_changed": [], "commands_run": [], "questions": [],
                "open_issues": []}
        self.assertEqual(agent.validate_result(good), [])
        self.assertTrue(agent.validate_result({**good, "status": "BLOCKED"}))
        self.assertTrue(agent.validate_result({**good, "extra": 1}))
        self.assertTrue(agent.validate_result({**good, "commands_run": [{"command": "x", "outcome": "ok"}]}))


@unittest.skipUnless(ACCEPTANCE_RUNS.is_dir(), "Phase 0 evidence not present")
class GoldenStreamTests(unittest.TestCase):
    """Parse real agy 1.5.9 streams recorded in Phase 0."""

    def parse(self, case):
        raw = (ACCEPTANCE_RUNS / case / "stdout.bin").read_bytes().decode("utf-8")
        recorded_repo = json.loads((ACCEPTANCE_RUNS / case / "meta.json").read_text(encoding="utf-8"))["repo"]
        return agent.parse_stream(raw, Path(recorded_repo))  # paths in the stream are as recorded

    def test_deny_rule_continues(self):  # A2h
        p = self.parse("a2h")
        states = [(c["command"], c["state"]) for c in p["commands"]]
        self.assertIn(("git log -1", "DENIED_BY_RULE"), states)
        self.assertIn(("node test.js", "DONE"), states)
        self.assertIsNone(p["aborted_on"])
        self.assertEqual(agent.extract_result(p)["status"], "INVALID")  # plain-text answer, no schema

    def test_ask_denial_aborts(self):  # A2c-stream
        p = self.parse("a2c-stream")
        self.assertEqual(p["aborted_on"], {"tool": "run_command", "target": "git status"})
        self.assertEqual(agent.extract_result(p)["status"], "MISSING")

    def test_production_run_is_valid(self):  # A4
        p = self.parse("a4")
        res = agent.extract_result(p)
        self.assertEqual(res["status"], "VALID")
        self.assertEqual(res["data"]["status"], "DONE")
        self.assertEqual([c["exit_code"] for c in p["commands"]], [0])
        self.assertEqual(sorted(p["files_written"]), ["src/greet.js", "test.js"])
        self.assertGreater(p["commands"][0]["step"], p["last_write_step"])

    def test_base_settings_smoke(self):  # A2k
        p = self.parse("a2k")
        kinds = sorted(r["kind"] for r in p["refused"])
        self.assertEqual(kinds, ["rule", "rule", "rule"])
        self.assertEqual([c["exit_code"] for c in p["commands"] if c["state"] == "DONE"], [0, 0, 0])


class SimpleCommandTests(RoomCase):
    def test_new_allocates_sequential_ids(self):
        ids = [self.cli("new", f"task {i}")[1].split()[0] for i in range(3)]
        self.assertEqual(ids, ["T-0001", "T-0002", "T-0003"])
        text = (self.room / "tasks" / "T-0001" / "task.md").read_text(encoding="utf-8")
        meta, _ = config.parse_task(text)
        self.assertEqual(meta["title"], "task 0")

    def test_pause_status_unpause(self):
        self.assertEqual(self.cli("pause", "lunch")[0], 0)
        rc, out, _ = self.cli("status")
        self.assertIn("paused: yes lunch", out)
        self.assertEqual(self.cli("unpause")[0], 0)
        self.assertIn("paused: no", self.cli("status")[1])

    def test_status_json(self):
        rc, out, _ = self.cli("status", "--json")
        data = json.loads(out)
        self.assertEqual(data["repo_branch"], "main")
        self.assertEqual(data["problems"], [])

    def test_unknown_task_and_bad_id(self):
        self.assertEqual(self.cli("show", "T-0099")[0], 4)
        self.assertEqual(self.cli("show", "7")[0], 2)

    def test_stop_with_nothing_running(self):
        self.assertEqual(self.cli("stop")[0], 3)

    def test_doctor(self):
        rc, out, _ = self.cli("doctor")
        self.assertEqual(rc, 0, out)
        self.assertIn("[OK] room.toml valid", out)
        self.assertIn("[OK] working tree clean", out)


class InitTests(unittest.TestCase):
    def test_init_creates_a_valid_room_and_refuses_twice(self):
        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d))
            room = Path(d) / "rooms" / "proj"
            rc, out, err = run_cli("--room", room, "init", "--repo", repo, "--agent", PY)
            self.assertEqual(rc, 0, err)
            cfg = config.load_room_config(room / "room.toml")
            self.assertEqual(Path(cfg["project"]["repo"]), repo.resolve())
            self.assertTrue((room / "agy-settings.base.json").exists())
            notes = (room / "PROJECT-NOTES.md").read_text(encoding="utf-8")
            self.assertIn("# Project notes - proj", notes)
            self.assertIn(str(repo.resolve()), notes)
            self.assertEqual(run_cli("--room", room, "init", "--repo", repo)[0], 3)

    def test_list_and_missing_room(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            repo = make_repo(home)
            real_rooms = store.ROOMS_HOME
            store.ROOMS_HOME = home / "rooms"
            try:
                self.assertIn("no rooms yet", run_cli("list")[1])
                self.assertEqual(run_cli("--room", "alpha", "init", "--repo", repo, "--agent", PY)[0], 0)
                self.assertEqual(run_cli("--room", "beta", "init", "--repo", repo, "--agent", PY)[0], 0)
                run_cli("--room", "beta", "pause")
                code, out, _ = run_cli("list")
            finally:
                store.ROOMS_HOME = real_rooms
            self.assertEqual(code, 0)
            lines = out.splitlines()
            self.assertTrue(lines[0].startswith("alpha: "))
            self.assertTrue(lines[1].startswith("beta: ") and lines[1].endswith("paused"))
        code, _, err = run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("--room is required", err)


if __name__ == "__main__":
    unittest.main()
