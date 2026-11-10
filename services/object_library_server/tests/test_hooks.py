#!/usr/bin/env python3
"""Tests for hooks/refresh_version.py: classification, restamp, and real git hook runs."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
import refresh_version as rv  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_data_change_excludes_version_file(self):
        self.assertEqual(rv.classify(["data/scenes/1/a.obj"]), (True, False))
        self.assertEqual(rv.classify(["data/VERSION.txt"]), (False, False))
        self.assertEqual(rv.classify(["data/model_index.db", "data/VERSION.txt"]), (True, False))

    def test_code_extensions(self):
        for f in ("server.py", "launch_server.bat", "hooks/install_hooks.sh", "a.PY"):
            self.assertEqual(rv.classify([f]), (False, True), f)
        for f in ("README.md", "docs/OLS接口.md", "config.json", "VERSION.txt", "dist/x.py"):
            self.assertEqual(rv.classify([f]), (False, False), f)
        # .py under data/ still counts as data content, not code
        self.assertEqual(rv.classify(["data/x.py"]), (True, False))

    def test_both(self):
        self.assertEqual(rv.classify(["data/models/1/a.obj", "server.py"]), (True, True))


class RestampTests(unittest.TestCase):
    def test_replaces_suffix_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "VERSION.txt"
            p.write_text("ResVersion 1.0.8-rel\nbuilt at 2026-11-07T23:58:31\n", "utf-8")
            self.assertTrue(rv.restamp(p, "abc1234", "2026-11-08T10:00:00"))
            self.assertEqual(p.read_text("utf-8"),
                             "ResVersion 1.0.8-abc1234\nbuilt at 2026-11-08T10:00:00\n")

    def test_appends_suffix_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "VERSION.txt"
            p.write_text("AppVersion 2.3.4\nbuilt at 2026-11-07T23:58:31\n", "utf-8")
            self.assertTrue(rv.restamp(p, "abc1234", "2026-11-08T10:00:00"))
            self.assertEqual(p.read_text("utf-8"),
                             "AppVersion 2.3.4-abc1234\nbuilt at 2026-11-08T10:00:00\n")

    def test_missing_or_unrecognized_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(rv.restamp(Path(tmp) / "nope.txt", "abc1234", "t"))
            p = Path(tmp) / "VERSION.txt"
            p.write_text("garbage\n", "utf-8")
            self.assertFalse(rv.restamp(p, "abc1234", "t"))


@unittest.skipIf(shutil.which("git") is None, "git not available")
class HookIntegrationTests(unittest.TestCase):
    """Run the real hook in a throwaway repo; verify follow-up commit and no recursion."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        shutil.copytree(ROOT / "hooks", self.repo / "hooks")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "hook-test")
        self.git("config", "core.hooksPath", "hooks")
        (self.repo / "VERSION.txt").write_text("AppVersion 1.0.0\nbuilt at 2026-01-01T00:00:00\n", "utf-8")
        (self.repo / "data").mkdir()
        (self.repo / "data" / "VERSION.txt").write_text("ResVersion 1.0.0\nbuilt at 2026-01-01T00:00:00\n", "utf-8")
        self.commit_all("init")  # hooks/*.py count as code -> root VERSION restamped in a follow-up commit

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        env = dict(os.environ)
        env.pop(rv.GUARD, None)
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True, env=env).stdout.strip()

    def commit_all(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def commit_count(self):
        return int(self.git("rev-list", "--count", "HEAD"))

    def version_line(self, rel):
        return (self.repo / rel).read_text("utf-8").splitlines()[0]

    def test_data_change_creates_res_version_followup_commit(self):
        before_root = (self.repo / "VERSION.txt").read_text("utf-8")
        (self.repo / "data" / "scenes").mkdir()
        (self.repo / "data" / "scenes" / "a.txt").write_text("x\n", "utf-8")
        count = self.commit_count()
        self.commit_all("data change")
        self.assertEqual(self.commit_count(), count + 2)  # user commit + refresh commit
        line = self.version_line("data/VERSION.txt")
        self.assertRegex(line, r"^ResVersion 1\.0\.0-[0-9a-f]{7,}$")
        # suffix is the triggering commit's short hash, still visible in git log
        trigger = self.git("rev-parse", "--short", "HEAD~1")
        self.assertEqual(line, f"ResVersion 1.0.0-{trigger}")
        self.assertEqual(self.git("show", "--name-only", "--pretty=format:", "HEAD"), "data/VERSION.txt")
        self.assertIn("data/scenes/a.txt", self.git("show", "--name-only", "--pretty=format:", "HEAD~1"))
        self.assertEqual((self.repo / "VERSION.txt").read_text("utf-8"), before_root)

    def test_code_change_creates_app_version_follow_up_commit(self):
        before_res = (self.repo / "data" / "VERSION.txt").read_text("utf-8")
        (self.repo / "tool.py").write_text("print(1)\n", "utf-8")
        count = self.commit_count()
        self.commit_all("code change")
        self.assertEqual(self.commit_count(), count + 2)
        line = self.version_line("VERSION.txt")
        self.assertRegex(line, r"^AppVersion 1\.0\.0-[0-9a-f]{7,}$")
        trigger = self.git("rev-parse", "--short", "HEAD~1")
        self.assertEqual(line, f"AppVersion 1.0.0-{trigger}")
        self.assertEqual(self.git("show", "--name-only", "--pretty=format:", "HEAD"), "VERSION.txt")
        self.assertEqual((self.repo / "data" / "VERSION.txt").read_text("utf-8"), before_res)

    def test_doc_change_does_not_touch_versions(self):
        before_root = (self.repo / "VERSION.txt").read_text("utf-8")
        before_res = (self.repo / "data" / "VERSION.txt").read_text("utf-8")
        (self.repo / "README.md").write_text("hello\n", "utf-8")
        count = self.commit_count()
        self.commit_all("doc change")
        self.assertEqual(self.commit_count(), count + 1)
        self.assertEqual((self.repo / "VERSION.txt").read_text("utf-8"), before_root)
        self.assertEqual((self.repo / "data" / "VERSION.txt").read_text("utf-8"), before_res)

    def test_version_file_only_change_does_not_retrigger(self):
        (self.repo / "data" / "VERSION.txt").write_text("ResVersion 9.9.9-manual\nbuilt at x\n", "utf-8")
        count = self.commit_count()
        self.commit_all("manual version edit")
        self.assertEqual(self.commit_count(), count + 1)
        self.assertEqual(self.version_line("data/VERSION.txt"), "ResVersion 9.9.9-manual")


if __name__ == "__main__":
    unittest.main(verbosity=2)