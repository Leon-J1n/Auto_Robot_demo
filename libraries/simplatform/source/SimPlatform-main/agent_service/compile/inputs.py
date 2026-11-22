"""Structural readers for the planner artifacts the compiler consumes.

``planning/`` owns the authoritative ``SkillPlan`` and ``GroundedTask`` contracts.
The compiler only needs a handful of fields from each, and it must stay usable
against a persisted JSON file, so these dataclasses read the structure instead of
importing the planner models.  Anything the planner produces is accepted
directly as well: the compiler duck-types both shapes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CompilerSkillStep:
    step_id: str
    operation_id: str
    skill_name: str
    target_object: str | None = None
    reference_object: str | None = None
    semantic_target: str | None = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompilerSkillPlan:
    steps: tuple[CompilerSkillStep, ...]
    task_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompilerOperation:
    operation_id: str
    task_type: str


@dataclass(frozen=True)
class CompilerGroundedTask:
    operations: tuple[CompilerOperation, ...]
    scene_id: str | None = None
    instruction: str = ""


def skill_plan_from_mapping(raw: Mapping[str, Any]) -> CompilerSkillPlan:
    steps = tuple(
        CompilerSkillStep(
            step_id=str(item["step_id"]),
            operation_id=str(item["operation_id"]),
            skill_name=str(item["skill_name"]),
            target_object=item.get("target_object"),
            reference_object=item.get("reference_object"),
            semantic_target=item.get("semantic_target"),
            depends_on=tuple(item.get("depends_on") or ()),
        )
        for item in raw.get("steps", ())
    )
    return CompilerSkillPlan(steps=steps, task_types=tuple(str(t) for t in raw.get("task_types") or ()))


def grounded_task_from_mapping(raw: Mapping[str, Any]) -> CompilerGroundedTask:
    operations = tuple(
        CompilerOperation(
            operation_id=str(item["operation_id"]),
            task_type=_task_type_value(item["task_type"]),
        )
        for item in raw.get("operations", ())
    )
    return CompilerGroundedTask(
        operations=operations,
        scene_id=raw.get("scene_id"),
        instruction=str(raw.get("instruction") or ""),
    )


def load_skill_plan(path: str | Path) -> CompilerSkillPlan:
    return skill_plan_from_mapping(_read_json(path))


def load_grounded_task(path: str | Path) -> CompilerGroundedTask:
    return grounded_task_from_mapping(_read_json(path))


def _task_type_value(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _read_json(path: str | Path) -> Mapping[str, Any]:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
