"""Deterministic expansion of grounded operations into atomic skills."""
from __future__ import annotations

from .contracts import SkillPlan, SkillStep
from .recipes import RECIPE_DEFINITIONS, validate_plan
from .skills import REGISTRY


class RecipePlanner:
    @staticmethod
    def supports(task) -> bool:
        for operation in task.operations:
            if operation.task_type.value not in RECIPE_DEFINITIONS:
                return False
            if operation.task_type.value == "pick_and_place" and not (operation.source and operation.destination):
                return False
            if operation.task_type.value in {"open", "close"} and not (
                operation.target and operation.reference
            ):
                return False
            if operation.task_type.value in {"locate", "search", "grasp", "press", "move", "release"} and not (operation.target or operation.source):
                return False
        return True

    def plan(self, task) -> SkillPlan:
        if not self.supports(task):
            raise ValueError("unsupported_recipe")
        grounded = {entity.entity_id: entity.object_id for entity in task.entities}
        steps: list[SkillStep] = []
        for operation in task.operations:
            definition = RECIPE_DEFINITIONS[operation.task_type.value]
            for skill, target_role, reference_role, region in definition.build(operation):
                target_entity = getattr(operation, target_role) if target_role else None
                reference_entity = getattr(operation, reference_role) if reference_role else None
                target = grounded.get(target_entity) if target_entity else None
                reference = grounded.get(reference_entity) if reference_entity else None
                REGISTRY.require(skill)
                steps.append(SkillStep(
                    step_id=f"step-{len(steps) + 1}",
                    operation_id=operation.operation_id,
                    skill_name=skill,
                    target_object=target,
                    reference_object=reference,
                    semantic_target=region,
                    depends_on=[steps[-1].step_id] if steps else [],
                ))
        plan = SkillPlan(task_types=task.task_types, steps=steps)
        validate_plan(plan, task)
        return plan
