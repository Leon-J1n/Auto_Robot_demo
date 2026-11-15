"""Internal preplanned trajectories guarded by exact scene and request state."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping

import numpy as np


def planning_state_signature(runtime, checker) -> str:
    """Conservative cache guard, including movable world and collision policy.

    Time is omitted: planning depends on positions/configuration, not the wall
    clock. Geometry and filter changes invalidate the cache even at fixed qpos.
    """
    digest = hashlib.sha256()
    for owner, names in (
        (runtime.data, ("qpos", "qvel", "ctrl", "mocap_pos", "mocap_quat", "eq_active")),
        (runtime.model, ("body_pos", "body_quat", "body_parentid", "body_jntadr", "body_jntnum",
                         "jnt_pos", "jnt_axis", "jnt_type", "jnt_range", "site_pos", "site_quat",
                         "geom_pos", "geom_quat", "geom_size", "geom_type", "geom_bodyid",
                         "geom_contype", "geom_conaffinity", "geom_margin", "geom_gap",
                         "pair_geom1", "pair_geom2", "pair_margin", "pair_gap", "exclude_signature", "eq_data")),
    ):
        for name in names:
            value = np.asarray(getattr(owner, name, []))
            digest.update(name.encode())
            digest.update(str((value.shape, value.dtype)).encode())
            digest.update(value.tobytes())
    controller = getattr(runtime, "gripper_controller", None)
    for name in ("_holding", "_commanded_force", "_held_body_id", "_held_qpos_address", "_held_joint_type",
                 "_held_offset", "_held_relative_rotation"):
        value = getattr(controller, name, None)
        digest.update(name.encode())
        digest.update(value.tobytes() if isinstance(value, np.ndarray) else repr(value).encode())
    policy = (
        sorted(checker.robot_geom_ids),
        sorted(tuple(sorted(pair)) for pair in checker.baseline_allowed_pairs),
        sorted(getattr(checker, "allowed_target_geom_ids", None) or ()),
    )
    digest.update(repr(policy).encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class PreparedTrajectory:
    request: Mapping[str, Any]
    trajectory: Mapping[str, Any]
    selection: Mapping[str, Any]
    state_signature: str
