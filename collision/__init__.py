"""Collision checking services.

The default import path is MuJoCo-free. The legacy MuJoCo checker remains
available through lazy compatibility imports for old callers.
"""

from .simplatform_fcl import CollisionResult, SimPlatformFCLCollisionChecker

__all__ = [
    "CollisionResult",
    "MujocoCollisionChecker",
    "SimPlatformFCLCollisionChecker",
]


def __getattr__(name: str):
    if name in {"MujocoCollisionChecker", "MujocoCollisionResult"}:
        from .mujoco_checker import CollisionResult as MujocoCollisionResult, MujocoCollisionChecker
        return {"MujocoCollisionChecker": MujocoCollisionChecker,
                "MujocoCollisionResult": MujocoCollisionResult}[name]
    raise AttributeError(name)
