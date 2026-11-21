"""Phase 4: rework, accept, reject, caps, pause, and acceptance tests A7 (git isolation) and A8 (stop)."""
import json
import os
import subprocess
import time
import unittest
from pathlib import Path

from helpers import PY, TOOL_HOME, RoomCase, git, read_run, scenario

from connect_room import gitops, lock, proc


def wait_for(condition, timeout=30.0, interval=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(interval)
    return False


class LifecycleTests(RoomCase):
    def feedback(self, tid, n, text="Please also add a test."):
        (self.room / "tasks" / tid / "rounds" / str(n) / "feedback.md").write_text(text, encoding="utf-8")

    def test_rework_then_accept_makes_one_squash_commit(self):
        base = gitops.rev(self.repo)
        tid = self.new_task()
        self.assertEqual(self.dispatch(tid, "done_ok")[0], 0)
        code, out, err = self.cli("rework", tid)
        self.assertEqual(code, 0, err)
        code, _, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 3)
        self.assertIn("feedback", err)
        self.feedback(tid, 2)
        code, out, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 0, err)
        prompt = (self.room / "tasks" / tid / "rounds" / "2" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("Please also add a test.", prompt)
        self.assertIn("Round 1: run COMPLETED", prompt)
        code, out, err = self.cli("accept", tid, "-m", "feat(T-0001): add farewell")
        self.assertEqual(code, 0, err)
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{base}..main"), "1")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "feat(T-0001): add farewell")
        self.assertTrue(gitops.branch_exists(self.repo, f"room/{tid}"))
        st = json.loads((self.room / "state.json").read_text(encoding="utf-8"))
        self.assertIsNone(st["open_task"])

    def test_accept_without_changes(self):
        base = gitops.rev(self.repo)
        tid = self.new_task()
        self.dispatch(tid, "blocked")
        code, out, _ = self.cli("accept", tid, "-m", "nothing")
        self.assertEqual(code, 0)
        self.assertIn("no changes", out)
        self.assertEqual(gitops.rev(self.repo, "main"), base)

    def test_accept_refusals(self):
        tid = self.new_task()
        self.dispatch(tid, "done_ok")
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho lint failed\nexit 1\n", encoding="utf-8", newline="\n")
        code, _, err = self.cli("accept", tid, "-m", "feat: x")
        self.assertEqual(code, 3)
        self.assertIn("lint failed", err)
        self.assertEqual(gitops.current_branch(self.repo), f"room/{tid}")
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        hook.unlink()
        git(self.repo, "switch", "-q", "main")
        (self.repo / "other.txt").write_text("x", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "someone moved main")
        git(self.repo, "switch", "-q", f"room/{tid}")
        code, _, err = self.cli("accept", tid, "-m", "feat: x")
        self.assertEqual(code, 3)
        self.assertIn("moved", err)

    def test_reject_with_leftovers(self):
        tid = self.new_task()
        self.dispatch(tid, "writes_env")
        code, _, err = self.cli("reject", tid, "--reason", "secrets")
        self.assertEqual(code, 3)
        self.assertIn("stash-leftovers", err)
        code, out, err = self.cli("reject", tid, "--reason", "secrets", "--stash-leftovers")
        self.assertEqual(code, 0, err)
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        self.assertIn(f"foreman {tid} leftovers", git(self.repo, "stash", "list"))

    def test_round_cap_and_override(self):
        tid = self.new_task(extra="max_rounds = 1")
        self.dispatch(tid, "done_ok")
        code, _, err = self.cli("rework", tid)
        self.assertEqual(code, 3)
        self.assertIn("override-cap", err)
        code, _, err = self.cli("rework", tid, "--override-cap", "one more try approved")
        self.assertEqual(code, 0, err)
        self.feedback(tid, 2)
        self.assertEqual(self.dispatch(tid, "done_ok")[0], 0)
        events = (self.room / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("cap_override", events)

    def test_one_open_task_and_pause(self):
        t1 = self.new_task()
        t2 = self.new_task()
        self.dispatch(t1, "done_ok")
        code, _, err = self.dispatch(t2, "done_ok")
        self.assertEqual(code, 3)
        self.assertIn("is open", err)
        self.cli("reject", t1)
        self.cli("pause", "hold")
        code, _, err = self.dispatch(t2, "done_ok")
        self.assertEqual(code, 3)
        self.assertIn("paused", err)
        self.cli("unpause")
        self.assertEqual(self.dispatch(t2, "done_ok")[0], 0)

    def test_manual_verify(self):
        tid = self.new_task()
        self.dispatch(tid, "done_ok")
        code, out, err = self.cli("verify", tid)
        self.assertEqual(code, 0, err)
        self.assertIn("PASSED", out)
        self.assertTrue((self.room / "tasks" / tid / "rounds" / "1" / "verify-manual-1.json").exists())

    def test_show(self):
        tid = self.new_task()
        self.dispatch(tid, "done_ok")
        code, out, _ = self.cli("show", tid, "--diff")
        self.assertEqual(code, 0)
        self.assertIn("READY_FOR_REVIEW", out)
        self.assertIn("src/greet.js", out)


class AcceptanceA7GitIsolation(RoomCase):
    def test_a7(self):
        tid = self.new_task()
        # 1-2: a dirty tracked file, then an untracked file, are refused
        (self.repo / "README.md").write_text("edited by hand\n", encoding="utf-8")
        self.assertEqual(self.dispatch(tid, "done_ok")[0], 3)
        git(self.repo, "checkout", "--", "README.md")
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.dispatch(tid, "done_ok")[0], 3)
        (self.repo / "untracked.txt").unlink()
        # 3: room data never dirties the repo
        self.assertEqual(self.dispatch(tid, "done_ok")[0], 0)
        self.cli("status")
        self.cli("show", tid)
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        # 7: accept makes exactly one commit on main and keeps the branch
        base = gitops.rev(self.repo, "main")
        self.assertEqual(self.cli("accept", tid, "-m", "feat: a7")[0], 0)
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{base}..main"), "1")
        self.assertTrue(gitops.branch_exists(self.repo, f"room/{tid}"))
        # 4: a sensitive file is not committed and blocks the next dispatch
        t2 = self.new_task()
        self.dispatch(t2, "writes_env")
        self.assertEqual(read_run(self.room, t2)["git"]["snapshot"], "SKIPPED_SENSITIVE")
        self.assertNotIn(".env", git(self.repo, "ls-files").splitlines())
        self.cli("reject", t2, "--stash-leftovers")
        # 5-6: scope violation and agent commits are flagged
        t3 = self.new_task()
        self.dispatch(t3, "out_of_scope")
        self.assertIn("SCOPE_VIOLATION", read_run(self.room, t3)["warnings"])
        self.cli("reject", t3)
        t4 = self.new_task()
        self.dispatch(t4, "commits_itself")
        self.assertIn("AGENT_COMMITTED", read_run(self.room, t4)["warnings"])


class AcceptanceA8EmergencyStop(RoomCase):
    def start_dispatch(self, tid, state_dir):
        env = dict(os.environ, FAKE_AGENT_SCENARIO="spawn_tree", FAKE_AGENT_STATE_DIR=str(state_dir))
        return subprocess.Popen([PY, str(TOOL_HOME / "room.py"), "--room", str(self.room), "dispatch", tid],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait_running(self, state_dir):
        self.assertTrue(wait_for(lambda: (state_dir / "pids.json").exists()), "agent tree did not start")
        pids = json.loads((state_dir / "pids.json").read_text(encoding="utf-8"))
        self.assertTrue(wait_for(lambda: all((state_dir / f"hb{i}.txt").exists() for i in range(3))))
        return pids

    def assert_tree_dead(self, state_dir, pids):
        self.assertTrue(wait_for(lambda: not any(proc.is_alive(p) for p in pids), timeout=10), pids)
        sizes = [(state_dir / f"hb{i}.txt").stat().st_size for i in range(3)]
        time.sleep(1.0)
        self.assertEqual(sizes, [(state_dir / f"hb{i}.txt").stat().st_size for i in range(3)])

    def test_a8(self):
        tid = self.new_task()
        # 1-3: stop kills the whole tree; the dispatcher records INTERRUPTED and snapshots partial work
        s1 = Path(self._tmp.name) / "s1"
        dispatcher = self.start_dispatch(tid, s1)
        pids = self.wait_running(s1)
        code, out, err = self.cli("stop", "--reason", "A8")
        self.assertEqual(code, 0, err)
        _, d_err = dispatcher.communicate(timeout=60)
        self.assertEqual(dispatcher.returncode, 0, d_err)
        self.assert_tree_dead(s1, pids)
        run = read_run(self.room, tid, 1)
        self.assertEqual(run["run_status"], "INTERRUPTED")
        self.assertEqual(run["agent"]["stop_reason"], "A8")
        self.assertEqual(run["git"]["snapshot"], "COMMITTED")
        events = (self.room / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("stop_requested", events)
        self.assertIn("round_recorded", events)

        # 4-5: kill only the dispatcher; status reports the orphan; stop kills it and recovers the round
        self.assertEqual(self.cli("rework", tid)[0], 0)
        (self.room / "tasks" / tid / "rounds" / "2" / "feedback.md").write_text("again", encoding="utf-8")
        s2 = Path(self._tmp.name) / "s2"
        dispatcher = self.start_dispatch(tid, s2)
        pids = self.wait_running(s2)
        subprocess.run(["taskkill", "/PID", str(dispatcher.pid), "/F"], capture_output=True)
        dispatcher.communicate(timeout=30)
        code, out, _ = self.cli("status")
        self.assertIn("orphaned agent", out)
        code, out, err = self.cli("stop")
        self.assertEqual(code, 0, err)
        self.assertIn("recovered", out)
        self.assert_tree_dead(s2, pids)
        run = read_run(self.room, tid, 2)
        self.assertEqual(run["run_status"], "INTERRUPTED")
        self.assertTrue(run["recovered"])
        self.assertFalse((self.room / lock.LOCK_NAME).exists())
        self.assertEqual(gitops.dirty_entries(self.repo), [])

        # 6: work continues normally
        self.assertEqual(self.cli("rework", tid)[0], 0)
        (self.room / "tasks" / tid / "rounds" / "3" / "feedback.md").write_text("finish it", encoding="utf-8")
        code, out, err = self.dispatch(tid, "done_ok")
        self.assertEqual(code, 0, err)
        self.assertEqual(read_run(self.room, tid, 3)["run_status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
