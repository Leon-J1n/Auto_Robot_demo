"""Execution-profile loader for the deterministic macros.

Profile numbers are the only source of the post-grasp lift and post-release
retreat distances.  The planner and the model never supply them, which is what
keeps compilation reproducible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..protocol import ErrorCode

PROFILE_ROOT_ENV = "AGENT_SERVICE_CONFIG_ROOT"
BUNDLED_PROFILE_DIR = Path(__file__).resolve().parent / "profiles"

REQUIRED_FIELDS = (
    "post_grasp_lift_m",
    "lift_relation",
    "lift_frame",
    "post_release_retreat_m",
    "retreat_relation",
    "retreat_frame",
)

ALLOWED_FRAMES = frozenset({"world", "tool"})


@dataclass(frozen=True)
class ExecutionProfile:
    post_grasp_lift_m: float
    lift_relation: str
    lift_frame: str
    post_release_retreat_m: float
    retreat_relation: str
    retreat_frame: str
    disable_end_effector: bool = True


def load_execution_profile(name: str = "default") -> ExecutionProfile:
    override = os.environ.get(PROFILE_ROOT_ENV)
    path = Path(override) / "execution_profiles" / f"{name}.json" if override else None
    if path is None or not path.is_file():
        path = BUNDLED_PROFILE_DIR / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"{ErrorCode.EXECUTION_PROFILE_INVALID}: no profile named {name!r}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    disable_end_effector = raw.get("disable_end_effector", True)
    if not isinstance(disable_end_effector, bool):
        raise ValueError(
            f"{ErrorCode.EXECUTION_PROFILE_INVALID}: disable_end_effector must be boolean"
        )
    missing = [key for key in REQUIRED_FIELDS if key not in raw]
    if missing:
        raise ValueError(f"{ErrorCode.EXECUTION_PROFILE_INVALID}: missing required fields {missing}")
    for key in ("lift_frame", "retreat_frame"):
        if raw[key] not in ALLOWED_FRAMES:
            raise ValueError(
                f"{ErrorCode.EXECUTION_PROFILE_INVALID}: {key} must be one of {sorted(ALLOWED_FRAMES)}"
            )
    return ExecutionProfile(
        post_grasp_lift_m=float(raw["post_grasp_lift_m"]),
        lift_relation=str(raw["lift_relation"]),
        lift_frame=str(raw["lift_frame"]),
        post_release_retreat_m=float(raw["post_release_retreat_m"]),
        retreat_relation=str(raw["retreat_relation"]),
        retreat_frame=str(raw["retreat_frame"]),
        disable_end_effector=disable_end_effector,
    )
