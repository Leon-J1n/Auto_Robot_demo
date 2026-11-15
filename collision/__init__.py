"""Collision checking services.

The default import path is MuJoCo-free. The legacy MuJoCo checker remains
available through lazy compatibility imports for old callers.
"""

from .backends.simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker
from .api import CollisionChecker, create_checker
from .scene.scene_model import CollisionScene
from .assets import AssetResolver
from .scene.scene_loader import load_scene
from .scene.robot_model import RobotModel

__all__ = [
    "CollisionResult",
    "MujocoCollisionChecker",
    "SimPlatformFCLCollisionChecker",
    "CollisionChecker",
    "CollisionScene",
    "create_checker",
    "load_scene",
    "AssetResolver",
    "RobotModel",
]


def __getattr__(name: str):
    if name in {"MujocoCollisionChecker", "MujocoCollisionResult"}:
        from .backends.mujoco import CollisionResult as MujocoCollisionResult, MujocoCollisionChecker
        return {"MujocoCollisionChecker": MujocoCollisionChecker,
                "MujocoCollisionResult": MujocoCollisionResult}[name]
    raise AttributeError(name)
