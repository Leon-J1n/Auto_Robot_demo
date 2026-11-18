"""Public kinematics API with optional simulator integrations."""

from .core import (
    IKBackend, IKError, IKRequest, IKResult, KinematicsService, RobotModel,
    SolverDescriptor, SolverRegistry,
)

__all__ = [
    "IKError", "IKBackend", "IKRequest", "IKResult", "RobotModel",
    "SolverDescriptor", "SolverRegistry", "KinematicsService", "MujocoKinematics",
]

def __getattr__(name):
    if name == "MujocoKinematics":
        from .backends.mujoco import MujocoKinematics
        return MujocoKinematics
    raise AttributeError(name)
