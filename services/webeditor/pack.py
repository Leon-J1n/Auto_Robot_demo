#!/usr/bin/env python3
"""Package the WebEditor into a runnable release under distribute/<version>/ (spec §11.6).

Usage:
    python pack.py 1.0.0-rel                # runs `npm run build`, then stages the bundle
    python pack.py 1.0.0-rel --no-build     # reuse the dist/ that is already there
    python pack.py 1.0.0 --dist-dir out     # write under ./out instead of ./distribute

Version strings: 大版本.中版本.小版本[-附加字符串] (suffix optional), e.g. 1.0.0 or 1.0.0-rel.

Output:
    distribute/<version>/WebEditor-<version>/    unpacked and runnable
    distribute/<version>/WebEditor-<version>.zip same content, top level = that folder

The bundle is a **release**, not a source drop: `local_gateway.py` serves `dist/`
itself (§11.7), so the target machine needs Python, aiohttp and a browser only.
Node, `editor/`, `tools/` and `tests/` stay out on purpose -- the runtime reads
none of them, and shipping them invites edits that never take effect.
"""
from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "WebEditor"
RELEASE_FILES = [
    "local_gateway.py",
    "speech_relay.py",
    "digest.py",
    "gateway_state.py",
    "features.json",
    "requirements.txt",
    "README.md",
    "launch_gateway.bat",
    "launch_gateway.py",
]
RELEASE_DIRS = ["dist", "config", "docs", "examples"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.~_-]+)?$")
ILLEGAL_IN_NAME = set('\\/:*?"<>|')


def validate_version(raw: str) -> str:
    version = raw.strip()
    if not version:
        sys.exit("[pack] version must not be empty")
    # The version doubles as a directory name, so path syntax beats the semver shape.
    if any(character in version for character in ILLEGAL_IN_NAME) or version in (".", ".."):
        sys.exit(f"[pack] version contains illegal path characters: {version!r}")
    if not VERSION_RE.match(version):
        sys.exit(f"[pack] invalid version: {version!r} "
                 "(expected 大版本.中版本.小版本[-附加字符串], e.g. 1.0.0 or 1.0.0-rel)")
    return version


def build_frontend() -> None:
    print("[pack] npm run build ...")
    # shell=True because `npm` is `npm.cmd` on Windows; the command itself is a constant.
    if subprocess.run("npm run build", cwd=ROOT, shell=True).returncode != 0:
        sys.exit("[pack] build failed; distribute/ was left untouched")


def stamp_version_file(path: Path, version: str, built: str) -> None:
    path.write_text(f"AppVersion {version}\nbuild at {built}\n", encoding="utf-8")


def require_sources() -> None:
    """Refuse to pack anything the bundle cannot run without.

    Called before the previous stage is deleted: `--no-build` against a `dist/` that
    exists but was never built is the one mistake that would otherwise cost a release.
    """
    for name in RELEASE_FILES:
        if not (ROOT / name).is_file():
            sys.exit(f"[pack] missing required file: {ROOT / name}")
    for name in RELEASE_DIRS:
        source = ROOT / name
        if name == "dist":
            if not (source / "index.html").is_file():
                sys.exit(f"[pack] {source / 'index.html'} is missing; run `npm run build`")
        elif not source.is_dir():
            sys.exit(f"[pack] missing required directory: {source}")


def stage_release(stage: Path, version: str, built: str) -> None:
    require_sources()
    stage.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_FILES:
        shutil.copy2(ROOT / name, stage / name)
    for name in RELEASE_DIRS:
        shutil.copytree(ROOT / name, stage / name, ignore=IGNORE)
    stamp_version_file(stage / "VERSION.txt", version, built)


def make_zip(folder: str, stage: Path, zip_path: Path) -> int:
    now = datetime.datetime.now()
    stamp = (now.year, now.month, now.day, now.hour, now.minute, now.second)
    files = sorted(path for path in stage.rglob("*") if path.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for path in files:
            info = zipfile.ZipInfo(f"{folder}/{path.relative_to(stage).as_posix()}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return len(files)


def human(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes} B" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GB"


def clear_previous(stage: Path, folder: str) -> None:
    """Delete the release this version had before, without ever risking it mid-delete.

    Windows lets a running process hold on to its working directory, so a plain
    `shutil.rmtree` of a bundle someone is serving empties it and *then* fails. The
    rename fails atomically instead, which leaves the in-use release intact.
    """
    if not stage.exists():
        return
    stale = stage.with_name(f"{folder}.old")
    if stale.exists():
        shutil.rmtree(stale)
    try:
        stage.rename(stale)
    except OSError as error:
        sys.exit(f"[pack] cannot take {stage} out of the way ({error}); "
                 f"stop the gateway running from it, or pack a different version")
    shutil.rmtree(stale)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="release version, e.g. 1.0.0 or 1.0.0-rel")
    parser.add_argument("--dist-dir", default="distribute", help="output root (default: ./distribute)")
    parser.add_argument("--no-build", action="store_true",
                        help="skip `npm run build` and pack the dist/ that is already there")
    args = parser.parse_args()

    version = validate_version(args.version)
    if not args.no_build:
        build_frontend()
    # Before the previous stage is deleted, so a failed pack never costs a release.
    require_sources()

    built = datetime.datetime.now().isoformat(timespec="seconds")
    folder = f"{NAME}-{version}"
    # --dist-dir is relative to the repository, not to whatever directory the
    # shell happens to be in, so a release always lands beside the sources it came from.
    out_root = Path(args.dist_dir)
    out_dir = (out_root if out_root.is_absolute() else ROOT / out_root) / version
    stage = out_dir / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous(stage, folder)

    print(f"[pack] staging {folder} into {out_dir} ...")
    stage_release(stage, version, built)
    zip_path = out_dir / f"{folder}.zip"
    count = make_zip(folder, stage, zip_path)

    print(f"[pack] {count} files -> {stage}")
    print(f"[pack] {zip_path} ({human(zip_path.stat().st_size)})")
    print(f"[pack] run it: cd {folder} && pip install -r requirements.txt && python local_gateway.py")


if __name__ == "__main__":
    main()
