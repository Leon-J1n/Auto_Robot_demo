from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from .commands import StrictModel


class ExecutionBundle(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    robot: str
    route: Literal["A", "B"]
    task_dir: str
    scene: str
    scene_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    interaction_registry: str
    commands: str

    @field_validator("task_dir", "scene", "interaction_registry", "commands")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("execution bundle paths must be absolute")
        return value


class ExecutionStepReport(StrictModel):
    runtime_step_id: str
    command_id: str
    source_skill_step_id: str | None = None
    # Historical reports call this field ``skill``; keep that wire name while
    # ExecutionFailure carries the more explicit ``skill_name`` field.
    skill: str
    target: str
    success: bool
    started_at: datetime
    finished_at: datetime
    duration_seconds: float = Field(ge=0)
    result: dict[str, Any] = Field(default_factory=dict)


class ExecutionFailure(StrictModel):
    command_id: str | None = None
    source_skill_step_id: str | None = None
    runtime_step_id: str | None = None
    skill_name: str | None = None
    target: str | None = None
    error_code: str
    error_message: str
    recoverable: bool = False


class ExecutionReport(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    success: bool
    robot: str
    scene: str
    started_at: datetime
    finished_at: datetime
    commands_total: int = Field(ge=0)
    commands_started: int = Field(ge=0)
    commands_completed: int = Field(ge=0)
    steps: list[ExecutionStepReport] = Field(default_factory=list)
    failure: ExecutionFailure | None = None


class ProcessFailure(StrictModel):
    success: Literal[False] = False
    error_code: str
    error_message: str
    report: ExecutionReport | None = None


# Compatibility alias for the previous control API.
RuntimeStepReport = ExecutionStepReport


def validate_bundle_consistency(bundle: ExecutionBundle) -> None:
    """Validate the persisted bundle against its referenced command document."""
    try:
        raw = json.loads(Path(bundle.commands).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"EXECUTION_BUNDLE_INCONSISTENT: cannot read commands: {exc}") from exc
    expected = {
        "scene": bundle.scene,
        "scene_fingerprint": bundle.scene_fingerprint,
        "registry": bundle.interaction_registry,
    }
    actual = {key: raw.get(key) for key in expected}
    if actual != expected:
        raise ValueError(f"EXECUTION_BUNDLE_INCONSISTENT: {actual!r} != {expected!r}")
