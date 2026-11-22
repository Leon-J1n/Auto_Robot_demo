from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ViewerMode(StrEnum):
    AUTO = "auto"
    STEP = "step"
    HEADLESS = "headless"


class ExecutionOptions(StrictModel):
    end_effector_site: str | None = None
    execution_mode: Literal["kinematic", "actuator"] = "kinematic"
    playback_fps: float = Field(default=60.0, gt=0)
    playback_speed: float = Field(default=2.0, gt=0)
    minimum_playback_duration: float = Field(default=1.0, ge=0)


class SkillCommand(StrictModel):
    command_id: str = Field(pattern=r"^[\w-]+$")
    source_skill_step_id: str | None = Field(default=None, pattern=r"^[\w-]+$")
    skill_name: Literal["locate", "move", "grasp", "release", "press", "pull", "push"]
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def target_is_present(self):
        target = self.parameters.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("parameters.target must be a non-empty string")
        return self


class CommandDocument(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    robot: str
    scene: str
    registry: str
    scene_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: ExecutionOptions = Field(default_factory=ExecutionOptions)
    request_defaults: dict[str, Any] = Field(default_factory=dict)
    commands: list[SkillCommand] = Field(min_length=1)

    @field_validator("scene", "registry")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("command document paths must be absolute")
        return value


def load_command_document(path: str | Path) -> CommandDocument:
    """Strictly load a formal v1 command document."""
    command_path = Path(path).expanduser().resolve()
    raw = json.loads(command_path.read_text(encoding="utf-8"))
    return CommandDocument.model_validate(raw)
