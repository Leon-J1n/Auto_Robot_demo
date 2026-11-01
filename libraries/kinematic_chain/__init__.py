"""Simulator-independent serial-chain geometry and RobotModel implementation."""
from .model import ChainRobotModel

__all__ = ["ChainRobotModel", "UrdfRobotModel"]


def __getattr__(name):
    if name == "UrdfRobotModel":
        from .urdf import UrdfRobotModel
        return UrdfRobotModel
    raise AttributeError(name)
