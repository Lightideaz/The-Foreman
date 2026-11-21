"""Phase 2: git operations and change checks against temporary repositories."""
import tempfile
import unittest
from pathlib import Path

from helpers import git, make_repo

from connect_room import checks, gitops


class GitCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = make_repo(Path(self._tmp.name))
        self.base = gitops.rev(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text="x\n"):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def fail_hook(self, name="pre-commit"):
        hook = self.repo / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\necho hook says no\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)


class StateTests(GitCase):
    def test_dirty_entries_cover_modified_staged_untracked_and_renamed(self):
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        self.write("src/greet.js", "changed\n")
        self.write("new file.txt")
        git(self.repo, "mv", "README.md", "READ.md")
        entries = gitops.dirty_entries(self.repo)
        self.assertEqual(len(entries), 3, entries)
        self.assertTrue(any(e.endswith("new file.txt") and e.startswith("??") for e in entries))
        self.assertTrue(any(e.startswith("R") for e in entries))

    def test_operation_in_progress(self):
        self.assertIsNone(gitops.operation_in_progress(self.repo))
        (self.repo / ".git" / "MERGE_HEAD").write_text(self.base, encoding="utf-8")
        self.assertEqual(gitops.operation_in_progress(self.repo), "merge")

    def test_changed_files_since_base(self):
        self.write("src/greet.js", "changed\n")
        self.write("src/مرحبا.js", "// arabic name\n")
        (self.repo / ".env.example").unlink()
        git(self.repo, "mv", "README.md", "DOCS.md")
        files = {f["path"]: f["status"] for f in gitops.changed_files(self.repo, self.base)}
        self.assertEqual(files["src/greet.js"], "M")
        self.assertEqual(files["src/مرحبا.js"], "?")
        self.assertEqual(files[".env.example"], "D")
        self.assertEqual(files["DOCS.md"], "R")

    def test_branch_helpers(self):
        gitops.create_branch(self.repo, "room/T-0001", "main")
        self.assertEqual(gitops.current_branch(self.repo), "room/T-0001")
        self.assertTrue(gitops.branch_exists(self.repo, "room/T-0001"))
        self.assertFalse(gitops.branch_exists(self.repo, "room/T-0002"))


class SnapshotTests(GitCase):
    def test_snapshot_commits_with_connect_room_identity_without_touching_config(self):
        self.write("src/a.js")
        snap = gitops.snapshot(self.repo, "room(T-0001): round 1 snapshot - x")
        self.assertEqual(snap["outcome"], "COMMITTED")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%an <%ae>"), "The Foreman <foreman@localhost>")
        self.assertEqual(git(self.repo, "config", "user.name"), "Test")
        self.assertEqual(gitops.dirty_entries(self.repo), [])

    def test_snapshot_without_changes(self):
        self.assertEqual(gitops.snapshot(self.repo, "m")["outcome"], "NO_CHANGES")

    def test_failing_hook_is_recorded_not_bypassed(self):
        self.fail_hook()
        self.write("src/a.js")
        snap = gitops.snapshot(self.repo, "m")
        self.assertEqual(snap["outcome"], "FAILED_HOOK")
        self.assertIn("hook says no", snap["output"])
        self.assertEqual(gitops.rev(self.repo), self.base)

    def test_squash_merge_and_its_failure_path(self):
        gitops.create_branch(self.repo, "room/T-0001", "main")
        self.write("src/a.js")
        gitops.snapshot(self.repo, "s1")
        self.write("src/b.js")
        gitops.snapshot(self.repo, "s2")
        self.assertTrue(gitops.has_diff(self.repo, self.base, "room/T-0001"))
        self.assertEqual(gitops.numstat(self.repo, self.base, "room/T-0001"), (2, 0))
        gitops.switch(self.repo, "main")
        self.fail_hook()
        failed = gitops.squash_merge(self.repo, "room/T-0001", "feat: x")
        self.assertFalse(failed["ok"])
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        self.assertEqual(gitops.rev(self.repo), self.base)
        (self.repo / ".git" / "hooks" / "pre-commit").unlink()
        ok = gitops.squash_merge(self.repo, "room/T-0001", "feat: x")
        self.assertTrue(ok["ok"])
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{self.base}..main"), "1")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "feat: x")

    def test_settle_stat_dirty_never_stages_real_changes(self):
        git(self.repo, "config", "core.autocrlf", "true")
        crlf = self.repo / "src" / "crlf.ts"
        crlf.write_bytes(b"export const a = 1;\r\nexport const b = 2;\r\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "crlf file")
        head_blob = git(self.repo, "rev-parse", "HEAD:src/crlf.ts")
        crlf.write_bytes(b"export const a = 1;\nexport const b = 2;\n")  # what a formatter hook might write
        (self.repo / "src" / "greet.js").write_text("real change\n", encoding="utf-8")
        gitops.settle_stat_dirty(self.repo)
        entries = gitops.dirty_entries(self.repo)
        self.assertIn(" M src/greet.js", entries)          # a real change stays unstaged
        self.assertNotIn(" M src/crlf.ts", entries)        # same content: no longer reported
        self.assertEqual(git(self.repo, "rev-parse", "HEAD:src/crlf.ts"), head_blob)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_stash_leftovers(self):
        self.write("left.txt")
        gitops.stash_leftovers(self.repo, "foreman T-0001 leftovers")
        self.assertEqual(gitops.dirty_entries(self.repo), [])
        self.assertIn("foreman T-0001 leftovers", git(self.repo, "stash", "list"))


class CheckTests(unittest.TestCase):
    FILES = [{"path": "src/a.js", "status": "M"}, {"path": "README.md", "status": "M"},
             {"path": "docs/x.md", "status": "A"}, {"path": "src/new.ts", "status": "R", "old_path": "lib/old.ts"},
             {"path": ".env", "status": "?"}, {"path": "config/.env.example", "status": "A"},
             {"path": "keys/server.PEM", "status": "A"}, {"path": "old.key", "status": "D"}]

    def test_scope(self):
        self.assertIsNone(checks.scope_violations(self.FILES, []))
        out = checks.scope_violations(self.FILES, ["src/", "docs"])
        # deleting a file outside the scope is a violation too
        self.assertEqual(out, [".env", "README.md", "config/.env.example", "keys/server.PEM", "lib/old.ts",
                               "old.key"])
        self.assertEqual(checks.scope_violations([{"path": "test.js", "status": "M"}], ["test.js"]), [])
        self.assertEqual(checks.scope_violations([{"path": "srcx/a", "status": "M"}], ["src"]), ["srcx/a"])

    def test_protected(self):
        self.assertEqual(checks.protected_touched(self.FILES, ["docs/", "README.md"]), ["README.md", "docs/x.md"])

    def test_sensitive(self):
        hits = checks.sensitive_files(self.FILES, [".env", ".env.*", "*.pem", "*.key"], [".env.example"])
        self.assertEqual(hits, [".env", "keys/server.PEM"])


if __name__ == "__main__":
    unittest.main()
