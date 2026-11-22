"""Deterministic execution layer for the agent service.

This package is the whole coupling surface between the agent service and the
simulator: kinematics, collision, scene indexing, gripper control, and trajectory
production. Nothing here imports `mujoco`, and nothing here draws a UI.

The world is not ours. The web gateway holds the authoritative simulation and this
layer connects to it over Live Python (`sim_platform.live`), so `LiveSceneRuntime`
attaches to an endpoint it is given and never launches an instance. Forward
kinematics, Jacobians, and contact queries are answered by the platform from the
loaded scene graph rather than from a URDF, each on a private copy of the world, so
evaluating a hypothetical configuration never disturbs the running scene.

Because every query is also a network round trip, the discipline throughout is to
batch and to commit once: `expand_joint_batch` + one `kinematics()` call instead of a
loop, a stop-angle search over hypothetical configurations before a single gripper
command, and a per-configuration cache in the collision checker. Writing to the live
world mid-search is what must be avoided -- it dirties collision sync and the next
query pays roughly 365 ms instead of 5 ms.
"""

from __future__ import annotations

from .collision import CollisionResult, LiveCollisionChecker
from .executor import (CommandFailure, ExecutionOutcome, execute_commands)
from .gripper import LiveGripperController
from .kinematics import IKError, LiveKinematics
from .live_runtime import (ARM_JOINT_COUNT, END_EFFECTOR_LINK, UR5E_JOINT_NAMES,
                           LiveSceneRuntime, RuntimeConnectionError,
                           resample_for_playback)
from .planners import (CollisionFreePlanner, CollisionPlanningError, DirectPlanner,
                       PlanningError)
from .scene_index import GRIPPER_PREFIX, SceneIndex
from .trajectory import (DEFAULT_MARKER_RADIUS, DEFAULT_MARKER_SPACING, PALETTE,
                         TrajectoryMarkers, gripper_path, path_length,
                         segment_markers, thin_by_arc_length, trajectory_color)
from .transforms import (axis_angle_matrix, matrix_to_quaternion, quaternion_mapping,
                         quaternion_to_matrix, rotation_error, rotation_mapping,
                         rpy_to_matrix, serializable_pose)

__all__ = [
    "ARM_JOINT_COUNT",
    "DEFAULT_MARKER_RADIUS",
    "DEFAULT_MARKER_SPACING",
    "END_EFFECTOR_LINK",
    "GRIPPER_PREFIX",
    "PALETTE",
    "UR5E_JOINT_NAMES",
    "CollisionFreePlanner",
    "CollisionPlanningError",
    "CollisionResult",
    "CommandFailure",
    "DirectPlanner",
    "ExecutionOutcome",
    "IKError",
    "LiveCollisionChecker",
    "LiveGripperController",
    "LiveKinematics",
    "LiveSceneRuntime",
    "PlanningError",
    "RuntimeConnectionError",
    "SceneIndex",
    "TrajectoryMarkers",
    "axis_angle_matrix",
    "execute_commands",
    "gripper_path",
    "matrix_to_quaternion",
    "path_length",
    "quaternion_mapping",
    "quaternion_to_matrix",
    "resample_for_playback",
    "rotation_error",
    "rotation_mapping",
    "rpy_to_matrix",
    "segment_markers",
    "serializable_pose",
    "thin_by_arc_length",
    "trajectory_color",
]
