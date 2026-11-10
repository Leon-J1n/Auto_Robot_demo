#!/usr/bin/env python3
"""OLS post-commit version refresh (invoked by hooks/post-commit).

Rules, evaluated against the files changed in the just-created commit:
- data/ changed (excluding data/VERSION.txt) -> restamp data/VERSION.txt
- code files changed (.py/.bat/.sh/... outside data/ and dist/) -> restamp VERSION.txt

Restamp keeps x.y.z, replaces/appends the suffix with the triggering commit's
short hash, and sets "built at" to now. The refreshed files go into a separate
follow-up commit, so the triggering commit stays visible in git log under that
hash. The follow-up commit only touches VERSION.txt files, which re-triggers
post-commit but matches neither rule; OLS_VERSION_HOOK_RUNNING additionally
blocks re-entry.
"""
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = "OLS_VERSION_HOOK_RUNNING"
CODE_EXTS = {
    ".py", ".bat", ".sh", ".cmd", ".ps1", ".js", ".ts", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".pl", ".lua", ".sql",
}
VERSION_LINE = re.compile(r"^(AppVersion|ResVersion)\s+(\d+\.\d+\.\d+)(?:-\S+)?\s*$", re.MULTILINE)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def changed_files():
    out = git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD")
    return [line for line in out.splitlines() if line]


def classify(files):
    """Return (data_changed, code_changed) for a list of repo-relative paths."""
    data = any(f.startswith("data/") and f != "data/VERSION.txt" for f in files)
    code = any(Path(f).suffix.lower() in CODE_EXTS
               and not f.startswith(("data/", "dist/")) for f in files)
    return data, code


def restamp(path, short_hash, built):
    """Rewrite path as '<prefix> x.y.z-<short_hash>\\nbuilt at <built>\\n'.

    Returns True if the file was written, False if missing/unrecognized.
    """
    try:
        text = path.read_text("utf-8")
    except OSError:
        print(f"[ols-hooks] missing {path}; skip", file=sys.stderr)
        return False
    m = VERSION_LINE.search(text)
    if not m:
        print(f"[ols-hooks] unrecognized version line in {path}; skip", file=sys.stderr)
        return False
    new_text = f"{m.group(1)} {m.group(2)}-{short_hash}\nbuilt at {built}\n"
    if new_text == text:
        return False
    path.write_text(new_text, "utf-8")
    return True


def main():
    if os.environ.get(GUARD):
        return 0
    data_changed, code_changed = classify(changed_files())
    if not (data_changed or code_changed):
        return 0
    short = git("rev-parse", "--short", "HEAD")
    built = datetime.datetime.now().isoformat(timespec="seconds")
    targets = []
    if data_changed:
        targets.append(ROOT / "data" / "VERSION.txt")
    if code_changed:
        targets.append(ROOT / "VERSION.txt")
    updated = [p for p in targets if restamp(p, short, built)]
    if not updated:
        return 0
    git("add", "--", *[p.relative_to(ROOT).as_posix() for p in updated])
    env = dict(os.environ)
    env[GUARD] = "1"
    subprocess.run(["git", "commit", "-m", f"chore: refresh VERSION.txt (triggered by {short})"],
                   cwd=ROOT, check=True, env=env)
    names = ", ".join(p.relative_to(ROOT).as_posix() for p in updated)
    print(f"[ols-hooks] refreshed {names} (suffix {short}), follow-up commit {git('rev-parse', '--short', 'HEAD')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())