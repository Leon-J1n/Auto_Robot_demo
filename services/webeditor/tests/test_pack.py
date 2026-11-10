"""pack.py: version validation, the release manifest and the zip layout (spec §11.6).

npm is never run here -- building is the developer's job, and pack.py only copies the
`dist/` that already exists (Q95). Each test points `pack.ROOT` at a fake repository so
staging exercises the real manifest without touching the real one.
"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pack


def build_fake_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in pack.RELEASE_FILES:
        (root / name).write_text(f"content of {name}\n", encoding="utf-8")
    for name in pack.RELEASE_DIRS:
        target = root / name
        target.mkdir(parents=True)
        if name == "dist":
            (target / "assets").mkdir()
            (target / "index.html").write_text("<html>spa shell</html>", encoding="utf-8")
            (target / "assets" / "index-abc.js").write_text("bundle", encoding="utf-8")
        else:
            (target / "one.txt").write_text(name, encoding="utf-8")
    # Never part of a release, wherever they sit.
    (root / "config" / "__pycache__").mkdir()
    (root / "config" / "__pycache__" / "constant_config.pyc").write_text("x", encoding="utf-8")
    (root / "docs" / ".DS_Store").write_text("x", encoding="utf-8")
    return root


class PackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        fake = build_fake_repo(Path(self.directory.name) / "repo")
        self.output = Path(self.directory.name) / "out"
        self.addCleanup(setattr, pack, "ROOT", pack.ROOT)
        pack.ROOT = fake

    def stage(self, version="1.0.0-rel"):
        stage = self.output / version / f"{pack.NAME}-{version}"
        pack.stage_release(stage, version, "2026-10-16T12:00:00")
        return stage

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["pack.py", *argv]), contextlib.redirect_stdout(io.StringIO()):
            pack.main()

    def test_version_format_accepts_a_release_tag(self):
        self.assertEqual(pack.validate_version("1.0.0"), "1.0.0")
        self.assertEqual(pack.validate_version(" 1.0.0-rel "), "1.0.0-rel")
        self.assertEqual(pack.validate_version("2.10.3-dev.1"), "2.10.3-dev.1")

    def test_version_format_rejects_anything_not_a_version(self):
        for value in ("", "   ", "1.0", "1.0.0.0", "v1.0.0", "1.0.0 rel", "1.0.0+build"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                pack.validate_version(value)

    def test_version_format_rejects_path_syntax(self):
        # The version becomes a directory name, so this check runs before the shape check.
        for value in ("..", "../1.0.0", "1.0.0/rel", "1.0.0\\rel", "a:b", "1.0.0*"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                pack.validate_version(value)

    def test_stage_carries_the_manifest_and_stamps_its_version(self):
        stage = self.stage()
        for name in pack.RELEASE_FILES:
            self.assertTrue((stage / name).is_file(), name)
        for name in pack.RELEASE_DIRS:
            self.assertTrue((stage / name).is_dir(), name)
        self.assertEqual((stage / "dist" / "index.html").read_text("utf-8"), "<html>spa shell</html>")
        self.assertEqual((stage / "VERSION.txt").read_text("utf-8").splitlines()[0],
                         "AppVersion 1.0.0-rel")
        self.assertFalse((stage / "config" / "__pycache__").exists())
        self.assertFalse((stage / "docs" / ".DS_Store").exists())

    def test_clear_previous_removes_the_release_it_replaces(self):
        stage = self.stage()
        pack.clear_previous(stage, "WebEditor-1.0.0-rel")
        self.assertFalse(stage.exists())
        self.assertFalse(stage.with_name("WebEditor-1.0.0-rel.old").exists())

    def test_clear_previous_keeps_a_release_that_is_still_in_use(self):
        stage = self.stage()
        with mock.patch.object(Path, "rename", side_effect=OSError("used by another process")):
            with self.assertRaises(SystemExit) as caught:
                pack.clear_previous(stage, "WebEditor-1.0.0-rel")
        self.assertIn("stop the gateway", str(caught.exception))
        self.assertTrue((stage / "local_gateway.py").is_file())

    def test_stage_refuses_to_ship_a_missing_build(self):
        (pack.ROOT / "dist" / "index.html").unlink()
        with self.assertRaises(SystemExit) as caught:
            self.stage()
        self.assertIn("npm run build", str(caught.exception))

    def test_main_guards_before_the_previous_stage_is_deleted(self):
        previous = self.output / "1.0.0" / "WebEditor-1.0.0-rel"
        previous.mkdir(parents=True)
        sentinel = previous / "SENTINEL.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        shutil.rmtree(pack.ROOT / "dist")
        with self.assertRaises(SystemExit) as caught:
            self.run_main("1.0.0", "--no-build", "--dist-dir", str(self.output))
        self.assertIn("npm run build", str(caught.exception))
        self.assertEqual(sentinel.read_text("utf-8"), "keep me")

    def test_main_writes_the_bundle_and_the_zip_next_to_it(self):
        # A relative --dist-dir is resolved against the repository, not the shell's cwd.
        self.run_main("0.1.0-rel", "--no-build", "--dist-dir", "out")
        release = pack.ROOT / "out" / "0.1.0-rel"
        stage = release / "WebEditor-0.1.0-rel"
        self.assertTrue((stage / "dist" / "index.html").is_file())
        with zipfile.ZipFile(release / "WebEditor-0.1.0-rel.zip") as archive:
            self.assertIn("WebEditor-0.1.0-rel/local_gateway.py", archive.namelist())

    def test_zip_wraps_every_file_in_the_release_folder(self):
        stage = self.stage()
        zip_path = self.output / "WebEditor-1.0.0-rel.zip"
        count = pack.make_zip(stage.name, stage, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), count)
            self.assertTrue(all(name.startswith(f"{pack.NAME}-1.0.0-rel/") for name in names))
            self.assertIn(f"{pack.NAME}-1.0.0-rel/dist/assets/index-abc.js", names)
            self.assertEqual({info.date_time for info in archive.infolist()}, {archive.infolist()[0].date_time})


if __name__ == "__main__":
    unittest.main()
