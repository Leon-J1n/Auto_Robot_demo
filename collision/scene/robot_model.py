"""Backend-neutral robot model metadata."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RobotModel:
    urdf: Path
    joint_names: tuple[str, ...]
    base_link: str = "base_link"
    tip_link: str = "tool0"
