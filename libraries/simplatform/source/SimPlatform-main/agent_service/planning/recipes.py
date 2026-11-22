from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .skills import REGISTRY


@dataclass(frozen=True)
class RecipeDefinition:
    task_type: str
    build: Callable


def _grasp(operation):
    return [("locate", "target", None, None), ("move", "target", None, "grasp_region"), ("grasp", "target", None, None)]


def _press(operation):
    return [("locate", "target", None, None), ("move", "target", None, "button_surface"), ("press", "target", None, None)]


def _pick_and_place(operation):
    return [("locate", "source", None, None), ("move", "source", None, "grasp_region"), ("grasp", "source", None, None), ("locate", "destination", None, None), ("move", "destination", "source", "container_interior"), ("release", "source", "destination", "container_interior")]


def _locate(operation): return [("locate", "target", None, None)]
def _search(operation): return [("search", "target", None, None)]
def _move(operation):
    target = "target" if operation.target else "source"
    return [("locate", target, None, None), ("move", target, "reference" if operation.reference else None, "relative_region" if operation.reference else "semantic_region")]
def _release(operation):
    target = "target" if operation.target else "source"
    return [("locate", target, None, None), ("release", target, "reference" if operation.reference else None, "semantic_region")]


def _open(operation):
    return [
        ("locate", "reference", None, None),
        ("move", "reference", None, "grasp_region"),
        ("grasp", "reference", None, None),
        ("pull", "target", "reference", None),
        ("release", "reference", None, None),
    ]


def _close(operation):
    return [
        ("locate", "reference", None, None),
        ("move", "reference", None, "grasp_region"),
        ("grasp", "reference", None, None),
        ("push", "target", "reference", None),
        ("release", "reference", None, None),
    ]


RECIPE_DEFINITIONS = {
    "grasp": RecipeDefinition("grasp", _grasp), "press": RecipeDefinition("press", _press),
    "pick_and_place": RecipeDefinition("pick_and_place", _pick_and_place), "locate": RecipeDefinition("locate", _locate),
    "search": RecipeDefinition("search", _search), "move": RecipeDefinition("move", _move),
    "release": RecipeDefinition("release", _release),
    "open": RecipeDefinition("open", _open), "close": RecipeDefinition("close", _close),
}


def validate_plan(plan, task) -> None:
    object_ids = {entity.object_id for entity in task.entities}
    operation_ids = [operation.operation_id for operation in task.operations]
    positions = {operation_id: index for index, operation_id in enumerate(operation_ids)}
    by_operation = {operation_id: [] for operation_id in operation_ids}
    last_operation_position = -1
    for step in plan.steps:
        definition = REGISTRY.require(step.skill_name)
        if step.operation_id not in by_operation:
            raise ValueError(f"skill references unknown operation: {step.operation_id}")
        if definition.requires_target and not step.target_object:
            raise ValueError(f"skill {step.skill_name} requires target_object")
        if step.target_object and step.target_object not in object_ids:
            raise ValueError(f"skill target is not grounded: {step.target_object}")
        if step.reference_object and step.reference_object not in object_ids:
            raise ValueError(f"skill reference is not grounded: {step.reference_object}")
        if step.semantic_target and definition.allowed_regions and step.semantic_target not in definition.allowed_regions:
            raise ValueError(f"unsupported region for {step.skill_name}: {step.semantic_target}")
        current_position = positions[step.operation_id]
        if current_position < last_operation_position:
            raise ValueError("skill steps must preserve operation order")
        last_operation_position = current_position
        by_operation[step.operation_id].append(step.skill_name)
    for operation in task.operations:
        actual = tuple(by_operation[operation.operation_id])
        expected = tuple(item[0] for item in RECIPE_DEFINITIONS[operation.task_type.value].build(operation))
        if actual != expected:
            raise ValueError(f"invalid skill recipe for {operation.operation_id}: expected {expected}, got {actual}")
        for dependency in operation.depends_on:
            if positions[dependency] >= positions[operation.operation_id]:
                raise ValueError("operation dependency order violated")
            if not by_operation[dependency] or not by_operation[operation.operation_id]:
                raise ValueError("operation dependency has no steps")
