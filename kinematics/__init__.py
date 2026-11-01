"""Robot kinematics and inverse-kinematics solvers."""

from .contracts import IKBackend, IKError, IKRequest, IKResult, RobotModel
from .registry import SolverDescriptor, SolverRegistry
from .service import KinematicsService

__all__ = ["IKError", "IKBackend", "IKRequest", "IKResult", "RobotModel",
           "SolverDescriptor", "SolverRegistry", "KinematicsService", "MujocoKinematics"]


def __getattr__(name):
    if name == "MujocoKinematics":
        from .mujoco_kinematics import MujocoKinematics
        return MujocoKinematics
    raise AttributeError(name)
