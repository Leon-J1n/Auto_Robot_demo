"""Generic semantic validation for model-composed atomic skill plans."""
from __future__ import annotations

from .context_builder import PlannerContext
from .contracts import GroundedTask, SkillPlan
from .skills import REGISTRY, AtomicSkillRegistry


def validate_semantic_plan(
    plan: SkillPlan,
    task: GroundedTask,
    context: PlannerContext,
    skill_registry: AtomicSkillRegistry = REGISTRY,
) -> None:
    """Validate grounding, affordances and a minimal sequential world state."""
    try:
        _validate(plan, task, context, skill_registry)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("semantic_plan_invalid:"):
            raise
        raise ValueError(f"semantic_plan_invalid: {message}") from exc


def _validate(plan, task, context, skill_registry) -> None:
    object_to_entity = {entity.object_id: entity.entity_id for entity in task.entities}
    entity_context = {entity.id: entity for entity in context.entities}
    operation_ids = [operation.operation_id for operation in task.operations]
    positions = {operation_id: index for index, operation_id in enumerate(operation_ids)}
    by_operation = {operation_id: [] for operation_id in operation_ids}
    last_position = -1

    located: set[str] = set()
    reached: set[tuple[str, str | None]] = set()
    held: str | None = None

    for step in plan.steps:
        definition = skill_registry.require(step.skill_name)
        if step.operation_id not in by_operation:
            raise ValueError(f"skill references unknown operation: {step.operation_id}")
        current_position = positions[step.operation_id]
        if current_position < last_position:
            raise ValueError("skill steps must preserve operation order")
        last_position = current_position
        by_operation[step.operation_id].append(step)

        if definition.requires_target and not step.target_object:
            raise ValueError(f"skill {step.skill_name} requires target_object")
        target = _resolve(step.target_object, object_to_entity, "target")
        reference = _resolve(step.reference_object, object_to_entity, "reference")
        if step.semantic_target and definition.allowed_regions and step.semantic_target not in definition.allowed_regions:
            raise ValueError(f"unsupported region for {step.skill_name}: {step.semantic_target}")
        target_facts = entity_context.get(target) if target else None
        if target and target_facts is None:
            raise ValueError(f"skill target is not in planner context: {target}")
        for affordance in definition.required_affordances:
            if target_facts is None or affordance not in target_facts.affordances:
                raise ValueError(f"{step.skill_name} requires {affordance} affordance on {target}")

        if step.skill_name in {"locate", "search"}:
            located.add(target)
        elif step.skill_name == "move":
            if target not in located:
                raise ValueError(f"move requires located target: {target}")
            region = step.semantic_target
            if region in {"grasp_region", "container_interior", "button_surface"}:
                if target_facts is None or region not in target_facts.regions:
                    raise ValueError(f"target {target} has no semantic region {region}")
            reached.add((target, region))
        elif step.skill_name == "grasp":
            if target not in located:
                raise ValueError(f"grasp requires located target: {target}")
            if (target, "grasp_region") not in reached:
                raise ValueError(f"grasp requires reached grasp_region for {target}")
            if held is not None and held != target:
                raise ValueError(f"cannot grasp {target} while holding {held}")
            held = target
        elif step.skill_name == "release":
            if held != target:
                raise ValueError(f"release requires held target: {target}")
            if step.semantic_target:
                destination = reference or target
                if (destination, step.semantic_target) not in reached:
                    raise ValueError(
                        f"release requires reached {step.semantic_target} for {destination}"
                    )
            held = None
        elif step.skill_name == "press":
            if target not in located:
                raise ValueError(f"press requires located target: {target}")
            if (target, "button_surface") not in reached:
                raise ValueError(f"press requires reached button_surface for {target}")
        elif step.skill_name in {"pull", "push"}:
            contact = reference or target
            if held != contact:
                raise ValueError(
                    f"{step.skill_name} requires established grasp/contact on {contact}"
                )
            if reference and f"part_of:{target}" not in entity_context[reference].relations:
                raise ValueError(
                    f"{step.skill_name} contact {reference} is not related to target {target}"
                )

    for operation in task.operations:
        if not by_operation[operation.operation_id]:
            raise ValueError(f"operation has no skill steps: {operation.operation_id}")
        for dependency in operation.depends_on:
            if dependency not in positions or positions[dependency] >= positions[operation.operation_id]:
                raise ValueError("operation dependency order violated")
            if not by_operation[dependency]:
                raise ValueError("operation dependency has no steps")


def _resolve(object_id: str | None, mapping: dict[str, str], role: str) -> str | None:
    if object_id is None:
        return None
    if object_id not in mapping:
        raise ValueError(f"skill {role} is not grounded: {object_id}")
    return mapping[object_id]
