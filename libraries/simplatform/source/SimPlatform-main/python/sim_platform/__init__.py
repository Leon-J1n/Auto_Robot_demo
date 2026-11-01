from importlib import import_module
from typing import TYPE_CHECKING

from .editor import EditorRobot, EditorRpcError, EditorSession
from .curobo_backend import CuroboCollisionBackend, CuroboUnavailableError
from .live import (Endpoint, EntityProxy, LaunchSpec, LinkProxy, LiveContactPair,
                   LiveControlError, LiveKinematicsBatch, LiveSession, ObjectRef, RobotProxy,
                   connect, connect_named, connect_or_launch, launch, list_instances,
                   parse_endpoint)

if TYPE_CHECKING:
    from .sdk import (ContactPair, Entity, JointInfo, KinematicsBatch, LiDAR, Robot,
                      SceneSnapshot, Simulator, State, Transform, World, api_version)

_SDK_EXPORTS = frozenset(
    {"ContactPair", "Entity", "JointInfo", "KinematicsBatch", "LiDAR", "Robot",
     "SceneSnapshot", "Simulator", "State", "Transform", "World", "api_version"}
)


def __getattr__(name: str) -> object:
    if name not in _SDK_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".sdk", __name__), name)
    globals()[name] = value
    return value

__all__ = ["ContactPair", "CuroboCollisionBackend", "CuroboUnavailableError", "EditorRobot",
           "EditorRpcError", "EditorSession", "Endpoint", "Entity", "EntityProxy",
           "JointInfo", "KinematicsBatch",
           "LaunchSpec", "LiDAR", "LinkProxy", "LiveContactPair", "LiveControlError",
           "LiveKinematicsBatch", "LiveSession",
           "ObjectRef", "Robot", "RobotProxy", "SceneSnapshot", "Simulator", "State",
           "Transform", "World",
           "api_version", "connect", "connect_named", "connect_or_launch", "launch",
           "list_instances", "parse_endpoint"]
