"""Phase 3: one dispatch round against the fake agent, scenario by scenario."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from helpers import FAIL_CMD, OK_CMD, RoomCase, git, read_run, scenario

from connect_room import gitops


class DispatchScenarios(RoomCase):
    def run_scenario(self, name, expect_code=0, **task_kwargs):
        tid = self.new_task(**task_kwargs)
        code, out, err = self.dispatch(tid, name)
        self.assertEqual(code, expect_code, f"stdout:\n{out}\nstderr:\n{err}")
        return tid, (read_run(self.room, tid) if expect_code == 0 else None), out

    def test_done_ok_is_ready_for_review(self):
        state_dir = Path(self._tmp.name) / "agent-state"
        tid = self.new_task()
        with scenario("done_ok", FAKE_AGENT_STATE_DIR=state_dir, CR_TEST_SECRET_TOKEN="s", CR_TEST_KEEP="k"):
            code, out, err = self.cli("dispatch", tid)
        self.assertEqual(code, 0, err)
        run = read_run(self.room, tid)
        self.assertEqual((run["run_status"], run["result_status"], run["agent_status"]), ("COMPLETED", "VALID", "DONE"))
        self.assertEqual(run["git"]["snapshot"], "COMMITTED")
        self.assertEqual(run["verify"]["status"], "PASSED")
        self.assertEqual(run["verify"]["source"], "room")
        self.assertEqual(run["verify"]["commands"][0]["agent_exit_code"], 0)
        self.assertEqual(run["verify"]["commands"][0]["room_exit_code"], 0)
        self.assertEqual(run["warnings"], [])
        self.assertEqual(run["verdict_hint"], "READY_FOR_REVIEW")
        self.assertEqual(run["agent"]["conversation_id"], "fake-conv-0001")
        self.assertIn("READY_FOR_REVIEW", out)
        self.assertEqual(gitops.current_branch(self.repo), f"room/{tid}")
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        task = json.loads((self.room / "tasks" / tid / "task-state.json").read_text(encoding="utf-8"))
        self.assertEqual(task["status"], "AWAITING_REVIEW")
        st = json.loads((self.room / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((st["open_task"], st["running"]), (tid, None))
        seen = json.loads((state_dir / "argv.json").read_text(encoding="utf-8"))
        argv = seen["argv"]
        for flag in ("stream-json", "accept-edits", "--sandbox", "--settings", "--json-schema", "--print-timeout"):
            self.assertIn(flag, argv)
        self.assertFalse(seen["env_has_secret"])
        self.assertTrue(seen["env_has_keep"])
        # sandbox model: no command allow rules (any allow rule would let the agent bypass the sandbox)
        self.assertEqual(seen["settings"]["toolPermission"], "proceed-in-sandbox")
        self.assertFalse([r for r in seen["settings"]["permissions"]["allow"] if r.startswith("command(")])
        self.assertTrue(seen["settings"]["permissions"]["deny"])
        self.assertIn("You are the implementing engineer", seen["prompt"])
        self.assertIn("Commands you may run", seen["prompt"])
        self.assertIn("only inside the sandbox", seen["prompt"])
        self.assertIn(OK_CMD, seen["prompt"])
        self.assertEqual(run["agent"]["permission_model"], "sandbox")
        report = (self.room / "tasks" / tid / "rounds" / "1" / "report.md").read_text(encoding="utf-8")
        self.assertIn("did the task", report)

    def test_room_verify_failure_overrides_agent_claim(self):
        _, run, _ = self.run_scenario("done_tests_fail", verify=[FAIL_CMD])
        self.assertEqual(run["verify"]["status"], "FAILED")
        self.assertEqual(run["verify"]["commands"][0]["agent_exit_code"], 0)
        self.assertEqual(run["verify"]["commands"][0]["room_exit_code"], 3)
        self.assertEqual(run["verdict_hint"], "NEEDS_ATTENTION")

    def test_blocked(self):
        _, run, out = self.run_scenario("blocked")
        self.assertIn("verify [not run]", out)
        self.assertEqual(run["agent_status"], "BLOCKED")
        self.assertEqual(run["git"]["snapshot"], "NO_CHANGES")
        self.assertEqual(run["verify"]["status"], "NOT_RUN")
        self.assertEqual(run["verdict_hint"], "NEEDS_ATTENTION")

    def test_invalid_answer_keeps_raw_text(self):
        tid, run, _ = self.run_scenario("invalid_json")
        self.assertEqual(run["result_status"], "INVALID")
        report = (self.room / "tasks" / tid / "rounds" / "1" / "report.md").read_text(encoding="utf-8")
        self.assertIn("trust me", report)

    def test_no_output(self):
        _, run, _ = self.run_scenario("no_output")
        self.assertEqual((run["run_status"], run["result_status"]), ("COMPLETED", "MISSING"))
        self.assertIn("AGENT_ERROR", run["warnings"])

    def test_crash_and_rejected_settings(self):
        _, run, _ = self.run_scenario("crash")
        self.assertEqual(run["run_status"], "CRASHED")

    def test_bad_settings(self):
        _, run, _ = self.run_scenario("bad_settings")
        self.assertEqual(run["run_status"], "CRASHED")
        self.assertIn("AGENT_SETTINGS_REJECTED", run["warnings"])

    def test_aborted_on_ask(self):
        _, run, out = self.run_scenario("aborted_on_ask")
        self.assertEqual(run["run_status"], "ABORTED_PERMISSION")
        self.assertEqual(run["activity"]["aborted_on"], {"tool": "run_command", "target": "rm -rf build"})
        self.assertEqual(run["git"]["snapshot"], "COMMITTED")
        self.assertEqual(run["verify"]["status"], "NOT_RUN")
        self.assertIn("ABORTED on run_command: rm -rf build", out)
        self.assertIn("aborted_permission", git(self.repo, "log", "-1", "--format=%s"))

    def test_deny_rule_continues(self):
        _, run, _ = self.run_scenario("deny_rule_continue")
        self.assertEqual((run["run_status"], run["agent_status"]), ("COMPLETED", "DONE"))
        self.assertIn("DENIED_BY_RULE", run["warnings"])
        self.assertEqual(run["verify"]["status"], "PASSED")
        self.assertEqual(run["verdict_hint"], "NEEDS_ATTENTION")

    def test_timeout_kills_and_snapshots(self):
        _, run, _ = self.run_scenario("hang", extra="timeout_minutes = 0.02")
        self.assertEqual(run["run_status"], "TIMED_OUT")
        self.assertEqual(run["git"]["snapshot"], "COMMITTED")
        self.assertLess(run["duration_s"], 30)

    def test_agent_commit_is_detected(self):
        _, run, _ = self.run_scenario("commits_itself")
        self.assertIn("AGENT_COMMITTED", run["warnings"])
        self.assertEqual(run["git"]["snapshot"], "NO_CHANGES")

    def test_out_of_scope(self):
        _, run, _ = self.run_scenario("out_of_scope")
        self.assertIn("SCOPE_VIOLATION", run["warnings"])
        self.assertEqual(run["checks"]["scope_violations"], ["README.md"])

    def test_sensitive_file_blocks_the_snapshot(self):
        _, run, _ = self.run_scenario("writes_env")
        self.assertEqual(run["git"]["snapshot"], "SKIPPED_SENSITIVE")
        self.assertEqual(run["checks"]["sensitive_files"], [".env"])
        self.assertIn("?? .env", gitops.dirty_entries(self.repo))

    def test_arabic_round_trip(self):
        tid, run, _ = self.run_scenario("arabic")
        self.assertEqual(run["result_summary"], "أضفت دالة الوداع")
        self.assertIn("مع السلامة، سارة", (self.repo / "src" / "greet.js").read_text(encoding="utf-8"))

    def test_no_scope_and_no_verify_are_warnings(self):
        _, run, _ = self.run_scenario("done_ok", allowed=(), verify=[])
        self.assertIn("NO_SCOPE", run["warnings"])
        self.assertIn("VERIFY_SKIPPED", run["warnings"])
        self.assertEqual(run["verify"]["status"], "SKIPPED")
        self.assertEqual(run["verdict_hint"], "NEEDS_ATTENTION")

    def test_refusals_before_launch(self):
        tid = self.new_task()
        (self.repo / "stray.txt").write_text("x", encoding="utf-8")
        code, _, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 3)
        self.assertIn("not clean", err)
        (self.repo / "stray.txt").unlink()
        toml = (self.room / "room.toml").read_text(encoding="utf-8")
        (self.room / "room.toml").write_text(toml.replace("[agent]\n", "[agent]\nmax_prompt_chars = 200\n"),
                                             encoding="utf-8")
        code, _, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 3)
        self.assertRegex(err, r"prompt is \d+ characters")
        self.assertFalse(gitops.branch_exists(self.repo, f"room/{tid}"))


class SandboxModelRefusesCommandAllowRules(RoomCase):
    def test_extra_allow_command_rule_is_refused_before_launch(self):
        tid = self.new_task(extra='extra_allow = ["command(git show)"]')
        code, _, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 2)
        self.assertIn("not allowed in the sandbox model", err)
        self.assertFalse(gitops.branch_exists(self.repo, f"room/{tid}"))


class AllowlistModel(RoomCase):
    permission_model = "allowlist"

    def test_allowlist_settings_and_prompt(self):
        state_dir = Path(self._tmp.name) / "agent-state"
        tid = self.new_task()
        with scenario("done_ok", FAKE_AGENT_STATE_DIR=state_dir):
            self.assertEqual(self.cli("dispatch", tid)[0], 0)
        seen = json.loads((state_dir / "argv.json").read_text(encoding="utf-8"))
        allow = seen["settings"]["permissions"]["allow"]
        self.assertNotIn("toolPermission", seen["settings"])
        self.assertTrue(any(r.startswith("command(regex:^git (status") for r in allow))
        self.assertTrue(any("print\\(1\\)" in r for r in allow))
        self.assertIn("exactly as written", seen["prompt"])


class VerifyAfterReview(RoomCase):
    room_run = "after_review"

    def test_room_run_waits_for_review_and_gates_accept(self):
        tid = self.new_task()
        code, out, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 0, err)
        run = read_run(self.room, tid)
        self.assertEqual(run["verify"]["status"], "AWAITING_ROOM_RUN")
        self.assertNotIn("room_exit_code", run["verify"]["commands"][0])
        self.assertEqual(run["verdict_hint"], "READY_FOR_REVIEW")
        self.assertIn(f"run `verify {tid}`", out)
        code, _, err = self.cli("accept", tid, "-m", "feat: x")
        self.assertEqual(code, 3)
        self.assertIn("no passing verification", err)
        (self.repo / "scratch.txt").write_text("x", encoding="utf-8")
        code, out, _ = self.cli("verify", tid)
        self.assertIn("does not count for accept", out)
        (self.repo / "scratch.txt").unlink()
        self.assertEqual(self.cli("accept", tid, "-m", "feat: x")[0], 3)
        code, out, err = self.cli("verify", tid)
        self.assertEqual(code, 0, err)
        self.assertIn("PASSED", out)
        code, out, err = self.cli("accept", tid, "-m", "feat: x")
        self.assertEqual(code, 0, err)

    def test_gate_can_be_skipped_with_a_logged_reason(self):
        tid = self.new_task()
        self.dispatch(tid, "done_ok")
        code, _, err = self.cli("accept", tid, "-m", "feat: x", "--skip-verify-gate", "verified by hand")
        self.assertEqual(code, 0, err)
        self.assertIn("verify_gate_skipped", (self.room / "events.jsonl").read_text(encoding="utf-8"))


class AgentEvidenceOnly(RoomCase):
    room_run = "never"

    def test_claim_mismatch_with_agent_evidence(self):
        tid = self.new_task()
        code, out, err = self.dispatch(tid, "done_but_cmd_failed")
        self.assertEqual(code, 0, err)
        run = read_run(self.room, tid)
        self.assertEqual(run["verify"]["source"], "agent-sandbox")
        self.assertEqual(run["verify"]["status"], "FAILED")
        self.assertIn("CLAIM_MISMATCH", run["warnings"])
        self.assertTrue(run["claim_mismatches"])


if __name__ == "__main__":
    unittest.main()
