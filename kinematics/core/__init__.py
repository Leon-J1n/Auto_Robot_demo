"""Core, simulator-independent kinematics contracts and services."""

from .contracts import IKBackend, IKError, IKRequest, IKResult, RobotModel
from .registry import SolverDescriptor, SolverRegistry, get_registry
from .service import KinematicsService

__all__ = [
    "IKBackend", "IKError", "IKRequest", "IKResult", "RobotModel",
    "SolverDescriptor", "SolverRegistry", "get_registry", "KinematicsService",
]
