"""Local UI (docs/UI-DESIGN.md): stack detection, room.py argv building, request checks, new room.py flags."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import PY, TOOL_HOME, RoomCase, make_repo, run_cli

from connect_room import cli, config
from foreman_ui import commands, server, stacks
from foreman_ui.commands import Commands, InvalidInput


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def package(scripts: dict) -> str:
    return json.dumps({"name": "app", "scripts": scripts})


class StackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def detect(self) -> dict:
        return stacks.detect(self.repo)

    def commands(self, found: dict) -> dict:
        return {v["command"]: v["checked"] for v in found["verify"]}

    def test_node_with_lockfile_suggests_existing_scripts_and_a_deny_rule(self):
        write(self.repo / "package.json", package({"typecheck": "tsc", "test": "vitest", "build": "vite", "dev": "x"}))
        write(self.repo / "pnpm-lock.yaml")
        found = self.detect()
        self.assertEqual(found["stacks"][0]["id"], "node")
        self.assertEqual(self.commands(found), {"pnpm run typecheck": True, "pnpm test": True, "pnpm run build": False})
        self.assertEqual([d["rule"] for d in found["deny"]], ["command(regex:^(pnpm|pnpx)( .*)?$)"])

    def test_node_without_lockfile_uses_npm_which_the_template_already_denies(self):
        write(self.repo / "package.json", package({"test": "jest", "lint": "eslint ."}))
        found = self.detect()
        self.assertEqual(self.commands(found), {"npm run lint": True, "npm test": True})
        self.assertEqual(found["deny"], [])

    def test_broken_package_json_still_detects_the_stack(self):
        write(self.repo / "package.json", "{not json")
        found = self.detect()
        self.assertEqual([s["id"] for s in found["stacks"]], ["node"])
        self.assertEqual(found["verify"], [])

    def test_python_pytest_or_unittest(self):
        write(self.repo / "pyproject.toml", "[tool.pytest.ini_options]\n")
        self.assertEqual(self.commands(self.detect()), {"python -m pytest": True})
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        self.assertEqual(self.commands(self.detect()), {"python -m unittest discover -s tests": True})
        self.assertIn("poetry", self.detect()["deny"][0]["rule"])

    def test_wrapper_wins_over_global_tool_and_globs_match(self):
        write(self.repo / "gradlew.bat")
        write(self.repo / "build.gradle")
        write(self.repo / "App.sln")
        found = self.detect()
        self.assertEqual([s["id"] for s in found["stacks"]], ["gradle", "dotnet"])
        self.assertEqual(found["stacks"][1]["evidence"], ["App.sln"])
        self.assertEqual(list(self.commands(found)), [r".\gradlew.bat test", "dotnet test"])

    def test_go_optional_command_is_unticked(self):
        write(self.repo / "go.mod", "module x\n")
        found = self.detect()
        self.assertEqual(self.commands(found), {"go test ./...": True, "go vet ./...": False})
        self.assertEqual(found["deny"][0]["rule"], "command(regex:^go (get|install|mod)( .*)?$)")

    def test_protected_path_suggestions_are_unticked_and_skip_vendored_folders(self):
        write(self.repo / ".github" / "workflows" / "ci.yml")
        write(self.repo / "Cargo.lock")
        write(self.repo / "src" / "db" / "migrations" / "0001.sql")
        write(self.repo / "node_modules" / "pkg" / "migrations" / "x.sql")
        found = self.detect()
        self.assertEqual([p["path"] for p in found["protect"]], [".github/", "Cargo.lock", "src/db/migrations/"])
        self.assertFalse(any(p["checked"] for p in found["protect"]))

    def test_empty_folder(self):
        self.assertEqual(self.detect(), {"stacks": [], "verify": [], "deny": [], "protect": []})

    def test_probe_matching_follows_full_match_rules(self):
        patterns = stacks.base_deny_patterns()
        self.assertTrue(stacks.already_denied("npm install", patterns))
        self.assertFalse(stacks.already_denied("cargo add serde", patterns))
        self.assertFalse(stacks.already_denied("x", ["(unclosed"]))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.rooms = self.root / "rooms"
        self.repo = make_repo(self.root)
        self.c = Commands(self.rooms, python=PY)

    def tearDown(self):
        self._tmp.cleanup()

    def test_room_names(self):
        for bad in ("", "My-App", "-x", "a b", "../x", "a" * 41, "x\\y"):
            with self.assertRaises(InvalidInput, msg=bad):
                commands.room_name(bad)
        self.assertEqual(commands.room_name("my-app-2"), "my-app-2")
        self.assertEqual(commands.suggest_room_name(r"D:\Code\My App (v2)"), "my-app-v2")
        self.assertEqual(commands.suggest_room_name("///"), "project")

    def test_init_argv_passes_values_as_flag_equals_value(self):
        argv = self.c.init("app", str(self.repo), "main", ["-rf npm test"], ["src/db/"],
                           ["command(regex:^cargo add( .*)?$)"])
        self.assertEqual(argv[:5], [PY, str(commands.ROOM_PY), "--room", str(self.rooms.resolve() / "app"), "init"])
        self.assertIn("--verify=-rf npm test", argv)
        self.assertIn("--protect=src/db/", argv)
        self.assertIn(f"--repo={self.repo.resolve()}", argv)

    def test_init_refuses_bad_values(self):
        cases = [
            dict(name="app", repo="relative/path", base="main"),
            dict(name="app", repo=str(self.root / "missing"), base="main"),
            dict(name="app", repo=str(self.repo), base="-main"),
            dict(name="app", repo=str(self.repo), base="a..b"),
            dict(name="app", repo=str(self.repo), base="main", verify=["one\ntwo"]),
            dict(name="app", repo=str(self.repo), base="main", verify="npm test"),
            dict(name="app", repo=str(self.repo), base="main", protect=["../outside"]),
            dict(name="app", repo=str(self.repo), base="main", protect=["C:\\abs"]),
            dict(name="app", repo=str(self.repo), base="main", deny=["command(git push)"]),
        ]
        for case in cases:
            with self.assertRaises(InvalidInput, msg=case):
                self.c.init(**case)

    def test_room_py_always_runs_as_a_console_program(self):
        """Under pythonw, each git/tasklist that room.py starts would flash its own console window."""
        folder = self.root / "py"
        (folder).mkdir()
        (folder / "python.exe").write_text("", encoding="utf-8")
        self.assertEqual(commands.console_python(str(folder / "pythonw.exe")), str(folder / "python.exe"))
        self.assertEqual(commands.console_python(str(folder / "python.exe")), str(folder / "python.exe"))
        self.assertEqual(commands.console_python(str(self.root / "pythonw.exe")), str(self.root / "pythonw.exe"))
        self.assertNotEqual(Path(Commands(self.rooms).python).name.lower(), "pythonw.exe")

    def test_only_allowlisted_subcommands(self):
        with self.assertRaises(InvalidInput):
            self.c._base("dispatch")
        with self.assertRaises(InvalidInput):
            self.c.show("app", "7")
        self.assertEqual(self.c.pause("app", "-x")[-2:], ["--", "-x"])
        self.assertEqual(self.c.stop("app", "")[-1], "--reason=")

    def test_end_to_end_through_room_py(self):
        rc, out, err = commands.run(self.c.init("app", str(self.repo), "main", [f'"{PY}" -c "print(1)"'],
                                                ["docs/"], ["command(regex:^cargo add( .*)?$)"]))
        self.assertEqual(rc, 0, err)
        cfg = config.load_room_config(self.rooms / "app" / "room.toml")
        self.assertEqual(cfg["verify"]["default_commands"], [f'"{PY}" -c "print(1)"'])
        self.assertEqual(cfg["policy"]["protected_paths"], ["docs/"])
        settings = json.loads((self.rooms / "app" / "agy-settings.base.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"]["deny"][-1], "command(regex:^cargo add( .*)?$)")

        rc, out, _ = commands.run(self.c.list())
        rows = json.loads(out)
        self.assertEqual([r["name"] for r in rows], ["app"])
        self.assertIsNone(rows[0]["latest"])

        rc, out, err = commands.run(self.c.pause("app", "-starts with a dash"))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.rooms / "app" / "PAUSE").read_text(encoding="utf-8"), "-starts with a dash")
        self.assertEqual(commands.run(self.c.unpause("app"))[0], 0)
        rc, _, err = commands.run(self.c.stop("app", "-x"))
        self.assertEqual(rc, 3, err)
        self.assertIn("no round is running", err)

        rc, out, _ = commands.run(self.c.doctor("app"))
        results = json.loads(out)
        self.assertTrue(all({"level", "check", "text", "hint", "url"} <= set(r) for r in results))
        self.assertIn("room", [r["check"] for r in results])


class RequestCheckTests(unittest.TestCase):
    TOKEN = "t0ken-abc"
    PORT = 5555

    def check(self, method="GET", path="/", query=None, **headers):
        base = {"host": f"127.0.0.1:{self.PORT}", "origin": None, "sec-fetch-site": None, "content-type": None,
                "x-foreman-token": None}
        base.update({k.replace("_", "-"): v for k, v in headers.items()})
        return server.check_request(method, path, query, base, self.TOKEN, self.PORT)

    def test_page_needs_the_query_token(self):
        self.assertIsNone(self.check(query=self.TOKEN))
        self.assertEqual(self.check(), "missing or wrong token")
        self.assertEqual(self.check(query="t0ken-abd"), "missing or wrong token")
        self.assertEqual(self.check(query="tökén"), "missing or wrong token")

    def test_api_needs_the_header_and_json_posts(self):
        self.assertIsNone(self.check(path="/api/rooms", x_foreman_token=self.TOKEN))
        self.assertEqual(self.check(path="/api/rooms", query=self.TOKEN), "missing or wrong token")
        self.assertEqual(self.check("POST", "/api/stop", x_foreman_token=self.TOKEN, content_type="text/plain"),
                         "POST needs application/json")
        self.assertIsNone(self.check("POST", "/api/stop", x_foreman_token=self.TOKEN,
                                     content_type="application/json; charset=utf-8"))

    def test_host_origin_and_cross_site_are_refused(self):
        ok = dict(query=self.TOKEN)
        self.assertEqual(self.check(host="localhost:5555", **ok), "wrong host")
        self.assertEqual(self.check(host="evil.example:5555", **ok), "wrong host")
        self.assertEqual(self.check(origin="http://evil.example", **ok), "foreign origin")
        self.assertIsNone(self.check(origin=f"http://127.0.0.1:{self.PORT}", **ok))
        self.assertEqual(self.check(sec_fetch_site="cross-site", **ok), "cross-site request")
        self.assertEqual(self.check("PUT", **ok), "method not allowed")
        self.assertEqual(self.check("POST", "/", **ok), "method not allowed")

    def test_inside(self):
        base = Path(tempfile.gettempdir())
        self.assertTrue(server.inside(base / "a" / "b", base))
        self.assertFalse(server.inside(base / ".." / "x", base))


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.rooms = Path(cls._tmp.name) / "rooms"
        cls.rooms.mkdir()
        cls.srv = server.UIServer(cls.rooms)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._tmp.cleanup()

    def request(self, method, path, body=None, headers=None, token_header=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=60)
        h = dict(headers or {})
        if token_header:
            h["X-Foreman-Token"] = self.srv.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r, payload

    def test_binds_loopback_with_a_random_port_and_strong_token(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertGreaterEqual(len(self.srv.token), 40)
        self.assertNotEqual(server.UIServer(self.rooms).token, self.srv.token)

    def test_page_and_static_files(self):
        r, body = self.request("GET", "/", token_header=False)
        self.assertEqual(r.status, 403)
        self.assertEqual(body, b"")
        r, body = self.request("GET", f"/?t={self.srv.token}", token_header=False)
        self.assertEqual(r.status, 200)
        self.assertIn(self.srv.token.encode(), body)
        self.assertNotIn(b"{{TOKEN}}", body)
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertFalse(any(k.lower().startswith("access-control") for k, _ in r.getheaders()))
        for path in ("/static/../server.py", "/static/stacks.toml", "/static/fonts/OFL.txt", "/static/x.css"):
            r, _ = self.request("GET", f"{path}?t={self.srv.token}", token_header=False)
            self.assertEqual(r.status, 404, path)
        r, body = self.request("GET", f"/static/fonts/IBMPlexSans-Regular-Latin1.woff2?t={self.srv.token}",
                               token_header=False)
        self.assertEqual((r.status, r.getheader("Content-Type"), body[:4]), (200, "font/woff2", b"wOF2"))

    def test_api_guards(self):
        r, _ = self.request("GET", "/api/info", token_header=False)
        self.assertEqual(r.status, 403)
        r, _ = self.request("OPTIONS", "/api/info", token_header=False,
                            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status, 405)
        r, _ = self.request("POST", "/api/heartbeat", body={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r, body = self.request("GET", "/api/info")
        self.assertEqual(r.status, 200)
        self.assertEqual(Path(json.loads(body)["rooms_dir"]), self.rooms.resolve())
        r, body = self.request("POST", "/api/pause", body={"room": "../x", "reason": ""})
        self.assertEqual(r.status, 400)
        r, body = self.request("POST", "/api/open-folder", body={"room": "nope"})
        self.assertEqual(r.status, 400)
        r, body = self.request("POST", "/api/detect", body={"path": "relative"})
        self.assertEqual(r.status, 400)
        r, _ = self.request("POST", "/api/heartbeat", body=None, headers={"Content-Length": "999999",
                                                                           "Content-Type": "application/json"})
        self.assertEqual(r.status, 400)

    def test_detect_reads_a_repository(self):
        repo = make_repo(Path(self._tmp.name) / "detect")
        (repo / "go.mod").write_text("module x\n", encoding="utf-8")
        r, body = self.request("POST", "/api/detect", body={"path": str(repo / "src")})
        d = json.loads(body)
        self.assertEqual(r.status, 200, d)
        self.assertTrue(d["is_repo"] and d["picked_subfolder"])
        self.assertEqual((d["branch"], d["name"], d["dirty"]), ("main", "repo", 1))   # go.mod is untracked
        self.assertEqual(d["stacks"][0]["id"], "go")


class RoomPyAdditionTests(RoomCase):
    def test_doctor_json_has_hints(self):
        rc, out, _ = self.cli("doctor", "--json")
        self.assertEqual(rc, 0, out)
        results = json.loads(out)
        self.assertIn({"level": "OK", "check": "clean_tree", "text": "working tree clean", "hint": "", "url": ""},
                      results)

    def test_show_json_and_list_json(self):
        tid = self.new_task()
        rc, out, err = self.cli("show", tid, "--json")
        self.assertEqual(rc, 0, err)
        d = json.loads(out)
        self.assertEqual((d["task"], d["title"], d["round"], d["run"]), (tid, "Test task", 0, None))
        rows = cli.list_data(self.room.parent)
        self.assertEqual(rows[0]["latest"]["task"], tid)
        self.assertEqual(rows[0]["max_rounds"], 3)

    def test_check_reports_a_missing_agent_with_a_link(self):
        rc, out, _ = run_cli("check", "--json", "--agent", str(self.root / "missing.exe"))
        self.assertEqual(rc, 2)
        agent = next(r for r in json.loads(out) if r["check"] == "agent")
        self.assertEqual(agent["level"], "FAIL")
        self.assertTrue(agent["url"].startswith("https://") and agent["hint"])
        rc, out, _ = run_cli("check", "--agent", PY)
        self.assertIn("[OK] python", out)

    def test_set_list_fills_only_the_named_key(self):
        text = "default_commands = []   # c\nprotected_paths = []\n"
        filled = cli._set_list(text, "default_commands", ["a \"b\"", "c'd"])
        self.assertTrue(filled.startswith("default_commands = ['a \"b\"', \"c'd\"]   # c"))
        self.assertIn("protected_paths = []", filled)

    def test_init_rejects_multiline_values(self):
        with tempfile.TemporaryDirectory() as d:
            rc, _, err = run_cli("--room", Path(d) / "r", "init", "--repo", self.repo, "--verify", "a\nb")
            self.assertEqual(rc, 2, err)
            self.assertFalse((Path(d) / "r").exists())


class NeutralityTests(unittest.TestCase):
    def test_ui_code_is_project_neutral(self):
        """Stack names live in foreman_ui/stacks.toml only; everything else stays project-neutral."""
        ui = TOOL_HOME / "foreman_ui"
        files = [TOOL_HOME / "foreman_ui.py", *ui.glob("*.py"), *(ui / "static").glob("*.*")]
        self.assertGreater(len(files), 6)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?i)pnpm|turbo\.json|husky|[A-Z]:\\Users\\", text),
                              f"project-specific text in {path.name}")


if __name__ == "__main__":
    unittest.main()
