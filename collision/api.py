"""Stable collision checker factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .scene.scene_model import CollisionScene, load_scene


def create_checker(backend: str, *, scene: CollisionScene | str | Path | None = None, **options: Any):
    """Create a backend checker without exposing backend module details."""
    name = backend.strip().lower().replace("-", "_")
    if isinstance(scene, (str, Path)):
        scene = load_scene(scene)
    if name in {"fcl", "simplatform_fcl", "simplatform"}:
        from .backends.simplatform_fcl import SimPlatformFCLCollisionChecker
        if scene is not None:
            options.setdefault("scene_file", scene.source)
            options.setdefault("scene_data", scene.document)
        return SimPlatformFCLCollisionChecker(**options)
    if name in {"mujoco", "mu_joco"}:
        from .backends.mujoco import MujocoCollisionChecker
        if "runtime" not in options:
            raise ValueError("The MuJoCo collision backend requires runtime=")
        return MujocoCollisionChecker(options["runtime"])
    if name == "bullet":
        from .backends.bullet import BulletCollisionChecker
        return BulletCollisionChecker(**options)
    raise ValueError(f"Unsupported collision backend: {backend!r}")


class CollisionChecker:
    create = staticmethod(create_checker)
