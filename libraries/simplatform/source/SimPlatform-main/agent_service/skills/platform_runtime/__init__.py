"""RobotSimPlatform runtime layer for the skills in this project.

This package is the whole coupling surface between the skills and the simulator. It
replaces the MuJoCo-specific `utils`, `kinematics`, `collision`, and `control` modules
of the original tree, and nothing here imports the Python `mujoco` package.

The scene's own robot is the source of truth. Forward kinematics, Jacobians, and
contact queries are answered by the platform from the loaded scene graph, not from a
URDF, so a scene without an accompanying URDF works the same way. Physics queries run
against the FCL collision backend.

Capability gaps are documented in the module that owns them rather than papered over:
`press_controller` has no contact-force readout, and `push_pull_controller` cannot
drive articulated scene objects.
"""

from __future__ import annotations

from .bootstrap import PROJECT_ROOT, install, sdk_path
from .collision import CollisionResult, PlatformCollisionChecker
from .gripper_controller import PlatformGripperController
from .kinematics import IKError, PlatformKinematics
from .press_controller import PlatformPressController
from .push_pull_controller import PlatformPushPullController, PushPullUnsupported
from .scene_index import SceneIndex
from .scene_runtime import (END_EFFECTOR_LINK, UR5E_JOINT_NAMES, SceneRobotRuntime)
from .trajectory_view import (DEFAULT_MARKER_RADIUS, DEFAULT_MARKER_SPACING, PALETTE,
                              EditorTrajectoryViewer, gripper_path, thin_by_arc_length,
                              trajectory_color)
from .transforms import (axis_angle_matrix, matrix_to_quaternion, quaternion_mapping,
                         quaternion_to_matrix, rotation_error, rotation_mapping,
                         rpy_to_matrix, serializable_pose)

__all__ = [
    "DEFAULT_MARKER_RADIUS",
    "DEFAULT_MARKER_SPACING",
    "END_EFFECTOR_LINK",
    "PALETTE",
    "PROJECT_ROOT",
    "UR5E_JOINT_NAMES",
    "CollisionResult",
    "EditorTrajectoryViewer",
    "IKError",
    "PlatformCollisionChecker",
    "PlatformGripperController",
    "PlatformKinematics",
    "PlatformPressController",
    "PlatformPushPullController",
    "PushPullUnsupported",
    "SceneIndex",
    "SceneRobotRuntime",
    "axis_angle_matrix",
    "gripper_path",
    "install",
    "matrix_to_quaternion",
    "quaternion_mapping",
    "quaternion_to_matrix",
    "rotation_error",
    "rotation_mapping",
    "rpy_to_matrix",
    "sdk_path",
    "serializable_pose",
    "thin_by_arc_length",
    "trajectory_color",
]
