"""Make the platform SDK and the skill tree importable.

Two paths are needed. The RobotSimPlatform Python SDK lives outside this project, so
its location is resolved here (overridable with ROBOTSIM_PLATFORM_PYTHON for a
checkout somewhere else). The skills themselves use absolute `skills.*` imports, so
the project root goes on the path as well -- that way a module can be imported
directly, not only through a skill entry point that has already fixed up sys.path.

Importing this module performs both insertions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_SDK_PATH = Path("F:/RobotSimPlatform/python")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def sdk_path() -> Path:
    """Where the sim_platform package lives."""
    override = os.environ.get("ROBOTSIM_PLATFORM_PYTHON")
    return Path(override).expanduser().resolve() if override else DEFAULT_SDK_PATH


def install() -> None:
    """Put the SDK and the project root on sys.path, idempotently."""
    root = sdk_path()
    if not (root / "sim_platform").is_dir():
        raise ImportError(
            f"RobotSimPlatform SDK not found at {root}. Set ROBOTSIM_PLATFORM_PYTHON "
            "to the directory containing the sim_platform package.")
    for entry in (root, PROJECT_ROOT):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


install()
