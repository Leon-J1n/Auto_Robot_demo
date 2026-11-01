"""MuJoCo implementation of the unified kinematics service."""
from __future__ import annotations
from .models.mujoco_model import MujocoRobotModel
from .service import KinematicsService


class MujocoKinematics(KinematicsService):
    def __init__(self, runtime, registry=None):
        self.runtime = runtime
        robot = MujocoRobotModel(runtime)
        self.model = robot.model
        self.site_id = robot.site_id
        self.qpos_indices = robot.qpos_indices
        self.dof_indices = robot.dof_indices
        super().__init__(robot, registry)
