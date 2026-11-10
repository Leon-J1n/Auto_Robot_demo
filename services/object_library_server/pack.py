#!/usr/bin/env python3
"""Package ObjectLibraryServer into dist/<version>/.

Usage:
    python pack.py 1.0.2                     # full release (data/ included);
                                             # resource version = data/VERSION.txt patch +1
    python pack.py 1.0.2 2.1.0               # full release with explicit resource version
    python pack.py 1.0.2 --no-data           # slim bundle, code version only (one version)

Version strings: 大版本.中版本.小版本[-附加字符串] (suffix optional), e.g. 1.0.2, 1.0.1-dev.

Output:
    dist/<version>/ObjectLibraryServer-<version>/   unpacked release content
    dist/<version>/ObjectLibraryServer-<version>[-res-<resversion>].zip

Packing stamps VERSION.txt files (build time = pack time) both in the bundle
and in the source tree (root VERSION.txt = code version, data/VERSION.txt =
resource version), so the server prints the current release versions at startup.
"""
import argparse
import datetime
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASE_FILES = ["server.py", "config.json", "init_index.py", "launch_server.bat", "README.md"]
RELEASE_DIRS = ["docs", "tests"]
DATA_DIR = "data"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.~_-]+)?$")


def validate_version(raw, label):
    version = raw.strip()
    if not VERSION_RE.match(version):
        sys.exit(f"[pack] invalid {label} version: {version!r} "
                 "(expected 大版本.中版本.小版本[-附加字符串], e.g. 1.0.2 or 1.0.1-dev)")
    return version


def sanitize_version(raw, label):
    version = raw.strip()
    if not version:
        sys.exit(f"[pack] {label} version must not be empty")
    if any(c in version for c in '\\/:*?"<>|') or version in (".", ".."):
        sys.exit(f"[pack] {label} version contains illegal path characters: {version!r}")
    return version


def read_resource_version(data_dir):
    """Current resource version from data/VERSION.txt ('ResVersion x.y.z')."""
    path = Path(data_dir) / "VERSION.txt"
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"^\s*(?:res)?version\s+(\S+)\s*$", text, re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else None


def bump_patch(version):
    """1.0.1 -> 1.0.2; keeps an existing -suffix (1.0.1-dev -> 1.0.2-dev)."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(-.*)?$", version)
    if not m:
        sys.exit(f"[pack] cannot bump invalid version: {version!r}")
    major, minor, patch, suffix = m.groups()
    return f"{major}.{minor}.{int(patch) + 1}{suffix or ''}"


def stamp_version_file(path, prefix, version, built):
    Path(path).write_text(f"{prefix} {version}\nbuilt at {built}\n", "utf-8")


def stage_release(stage, version, res_version, with_data, built):
    stage.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_FILES:
        src = ROOT / name
        if not src.is_file():
            sys.exit(f"[pack] missing required file: {src}")
        shutil.copy2(src, stage / name)
    for name in RELEASE_DIRS:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, stage / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    stamp_version_file(stage / "VERSION.txt", "AppVersion", version, built)
    if with_data:
        src = ROOT / DATA_DIR
        if not src.is_dir():
            sys.exit(f"[pack] missing required directory: {src}")
        shutil.copytree(src, stage / DATA_DIR,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        stamp_version_file(stage / DATA_DIR / "VERSION.txt", "ResVersion", res_version, built)


def make_zip(folder, stage, zip_path):
    now = datetime.datetime.now()
    stamp = (now.year, now.month, now.day, now.hour, now.minute, now.second)
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for p in files:
            arc = f"{folder}/{p.relative_to(stage).as_posix()}"
            info = zipfile.ZipInfo(arc, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(p, "rb") as f:
                zf.writestr(info, f.read())
    return len(files)


def human(nbytes):
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} B"
        nbytes /= 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="code release version, e.g. 1.0.2 or 1.0.1-dev")
    parser.add_argument("data_version", nargs="?",
                        help="resource (data) version; required only with data; "
                             "defaults to data/VERSION.txt patch +1")
    parser.add_argument("--no-data", action="store_true",
                        help="exclude data/ assets (code, docs and tests only)")
    parser.add_argument("--dist-dir", default="dist", help="output root (default: ./dist)")
    args = parser.parse_args()

    version = validate_version(sanitize_version(args.version, "code"), "code")
    with_data = not args.no_data
    if args.data_version is not None and not with_data:
        sys.exit("[pack] --no-data bundles carry a single version; drop the data_version argument")

    res_version = None
    if with_data:
        if args.data_version is not None:
            res_version = validate_version(sanitize_version(args.data_version, "resource"), "resource")
        else:
            current = read_resource_version(ROOT / DATA_DIR)
            if current is None:
                sys.exit("[pack] data/VERSION.txt missing or unreadable; provide an explicit data_version")
            res_version = bump_patch(current)
            print(f"[pack] resource version not given, bumping {current} -> {res_version}")

    built = datetime.datetime.now().isoformat(timespec="seconds")
    folder = f"ObjectLibraryServer-{version}"
    out_dir = Path(args.dist_dir) / version
    stage = out_dir / folder
    if stage.exists():
        shutil.rmtree(stage)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[pack] staging release {folder} "
          f"(code {version}, resource {res_version or 'n/a'}, data={'yes' if with_data else 'no'}) ...")
    stage_release(stage, version, res_version, with_data, built)
    zip_name = folder + (f"-res-{res_version}" if with_data else "") + ".zip"
    zip_path = out_dir / zip_name
    count = make_zip(folder, stage, zip_path)

    # stamp the source tree so startup prints match this release
    stamp_version_file(ROOT / "VERSION.txt", "AppVersion", version, built)
    if with_data:
        stamp_version_file(ROOT / DATA_DIR / "VERSION.txt", "ResVersion", res_version, built)

    print(f"[pack] {count} files -> {stage}")
    print(f"[pack] {zip_path} ({human(zip_path.stat().st_size)})")


if __name__ == "__main__":
    main()
