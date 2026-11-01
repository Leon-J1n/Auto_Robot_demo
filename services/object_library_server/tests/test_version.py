#!/usr/bin/env python3
"""Unit tests for version handling: VERSION.txt parsing (server) and pack.py version rules."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pack  # noqa: E402
import server  # noqa: E402


class ReadVersionFileTests(unittest.TestCase):
    def test_parses_app_and_res_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "VERSION.txt"
            p.write_text("AppVersion 1.0.1-dev\nbuilt at 2026-10-11T22:51:15\n", "utf-8")
            self.assertEqual(server.read_version_file(p), ("1.0.1-dev", "2026-10-11T22:51:15"))
            p.write_text("ResVersion 2.3.0\nbuilt at 2026-10-14T09:00:00\n", "utf-8")
            self.assertEqual(server.read_version_file(p), ("2.3.0", "2026-10-14T09:00:00"))

    def test_missing_file_is_unknown(self):
        self.assertEqual(server.read_version_file(Path(tempfile.gettempdir()) / "nope-VERSION.txt"),
                         ("unknown", "unknown"))

    def test_tolerates_blank_lines_and_missing_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "VERSION.txt"
            p.write_text("\nAppVersion 1.2.3\n", "utf-8")
            self.assertEqual(server.read_version_file(p), ("1.2.3", "unknown"))


class PackVersionTests(unittest.TestCase):
    def test_bump_patch(self):
        self.assertEqual(pack.bump_patch("1.0.1"), "1.0.2")
        self.assertEqual(pack.bump_patch("1.0.1-dev"), "1.0.2-dev")
        self.assertEqual(pack.bump_patch("0.0.0"), "0.0.1")

    def test_bump_patch_invalid_exits(self):
        with self.assertRaises(SystemExit):
            pack.bump_patch("1.0")

    def test_version_format_validation(self):
        self.assertEqual(pack.validate_version("1.0.2", "code"), "1.0.2")
        self.assertEqual(pack.validate_version("1.0.1-dev", "code"), "1.0.1-dev")
        self.assertEqual(pack.validate_version("1.0.1-rc.2_1", "code"), "1.0.1-rc.2_1")
        for bad in ("1.0", "1.0.2.1", "v1.0.2", "abc", "1.2.3-"):
            with self.assertRaises(SystemExit, msg=bad):
                pack.validate_version(bad, "code")

    def test_read_resource_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "VERSION.txt").write_text("ResVersion 1.0.1\nbuilt at x\n", "utf-8")
            self.assertEqual(pack.read_resource_version(tmp), "1.0.1")
            self.assertIsNone(pack.read_resource_version(Path(tmp) / "missing"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
