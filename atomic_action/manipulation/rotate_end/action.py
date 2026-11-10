"""Rotate the end effector around its terminal wrist joint."""
from __future__ import annotations
from typing import Any, Mapping
import numpy as np
from atomic_action.motion.move_joint.action import MoveJointAction

class RotateEndAction:
    def __init__(self, config: Mapping[str, Any] | None = None, *, robot_runtime: Any = None,
                 kinematics: Any = None, collision_checker: Any = None) -> None:
        self.config = config or {}
        self.robot_runtime = robot_runtime
        self.kinematics = kinematics
        self.collision_checker = collision_checker

    def execute(self, request: Mapping[str, Any]):
        if self.robot_runtime is None:
            raise RuntimeError('A robot runtime is required.')
        degree = request.get('degree')
        if isinstance(degree, bool) or not isinstance(degree, (int, float)) or not np.isfinite(float(degree)):
            raise ValueError('degree must be a finite number.')
        joints = np.asarray(self.robot_runtime.get_joint_positions(), dtype=float).copy()
        names = list(getattr(self.robot_runtime, 'joint_names', ()))
        index = names.index('wrist_3_joint') if 'wrist_3_joint' in names else len(joints) - 1
        joints[index] += np.deg2rad(float(degree))
        action = MoveJointAction(robot_runtime=self.robot_runtime, kinematics=self.kinematics,
                                 collision_checker=self.collision_checker)
        result = action.execute({'target': {'type': 'joint', 'joint_positions': joints.tolist()},
                                 'motion': {'path_type': 'joint'},
                                 'planning': {'mode': 'collision_free', 'allow_replan': True}})
        return result
