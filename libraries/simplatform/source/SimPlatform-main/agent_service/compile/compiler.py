"""Deterministically compile semantic plans into control command documents.

No model is ever called here, and no numeric distance originates in this module
except through an ``ExecutionProfile``.  The same ``SkillPlan`` plus the same
scene plan, interaction metadata and profile always produce byte-identical
commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..protocol import (
    CommandDocument,
    ErrorCode,
    ExecutionBundle,
    ExecutionOptions,
    SkillCommand,
)
from ..scene import ScenePlan, bind_authored_registry, build_generated_registry
from .execution_profile import ExecutionProfile, load_execution_profile
from .fingerprint import scene_plan_fingerprint
from .inputs import (
    CompilerGroundedTask,
    CompilerSkillPlan,
    grounded_task_from_mapping,
    load_grounded_task,
    load_skill_plan,
    skill_plan_from_mapping,
)

REGION_TO_ANCHOR = {
    "grasp_region": "grasp",
    "container_interior": "interior",
    "button_surface": "button_surface",
}

# The planner and recipe layers treat ``pull`` and ``push`` as separate skills,
# and ``SkillCommand.skill_name`` accepts both, because that is what the natural
# language distinguishes.  The control layer implements them as one bidirectional
# ``push_pull`` skill whose direction comes from the request.  The mapping is
# explicit rather than implied so neither layer has to guess: commands keep the
# semantic name, and the runtime resolves the implementing skill through
# ``control_skill_name``.
SKILL_TO_CONTROL_SKILL = {
    "locate": "locate",
    "move": "move",
    "grasp": "grasp",
    "release": "release",
    "press": "press",
    "pull": "push_pull",
    "push": "push_pull",
}

MANIPULATION_SKILLS = frozenset({"grasp", "release", "press", "pull", "push"})


def control_skill_name(skill_name: str) -> str:
    """Implementing control skill for a semantic command name."""
    try:
        return SKILL_TO_CONTROL_SKILL[skill_name]
    except KeyError as exc:
        raise ValueError(f"{ErrorCode.UNSUPPORTED_SKILL}: {skill_name}") from exc


def compile_execution_bundle(
    skill_plan: Any,
    grounded_task: Any,
    *,
    scene_plan: ScenePlan,
    output_dir: str | Path,
    route: str | None = None,
    authored_registry: Mapping[str, Any] | str | Path | None = None,
    robot: str = "ur5e",
    profile_name: str = "default",
) -> ExecutionBundle:
    """Compile a plan into a command document, bundle and step trace on disk."""
    if robot != "ur5e":
        raise ValueError(
            f"{ErrorCode.CONTROL_BACKEND_UNSUPPORTED_ROBOT}: control execution currently supports ur5e only"
        )
    plan = _as_skill_plan(skill_plan)
    task = _as_grounded_task(grounded_task)
    effective_route = str(route or scene_plan.route)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    scene = str(Path(scene_plan.base_scene).expanduser().resolve())
    fingerprint = scene_plan_fingerprint(scene_plan)
    registry = _interaction_registry(
        scene_plan, effective_route, authored_registry, scene=scene, fingerprint=fingerprint
    )
    registry_path = output / "interaction_registry.json"
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    objects = registry["objects"]
    operations = {item.operation_id: item for item in task.operations}
    profile = load_execution_profile(profile_name)

    commands: list[SkillCommand] = []
    traces: list[dict[str, Any]] = []
    current_trace: dict[str, Any] | None = None

    def add(skill: str, target: str, source_step: str, **parameters: Any) -> None:
        command = SkillCommand(
            command_id=f"command-{len(commands) + 1:03d}",
            source_skill_step_id=source_step,
            skill_name=skill,
            parameters={"target": target, **parameters},
        )
        commands.append(command)
        if current_trace is not None:
            # Level two of the trace chain: SkillPlan.step_id -> command_id.
            # The runtime appends runtime_step_id per command when it executes.
            entry = command.model_dump(mode="json")
            entry["control_skill"] = control_skill_name(skill)
            current_trace["generated_commands"].append(entry)

    for step in plan.steps:
        current_trace = {
            "skill_step_id": step.step_id,
            "operation_id": step.operation_id,
            "semantic_skill": step.skill_name,
            "target_object": step.target_object,
            "reference_object": step.reference_object,
            "resolved_anchor": None,
            "generated_commands": [],
        }
        traces.append(current_trace)
        if step.operation_id not in operations:
            raise ValueError(
                f"{ErrorCode.EXECUTION_BUNDLE_INCONSISTENT}: step {step.step_id} references unknown operation {step.operation_id}"
            )
        operation = operations[step.operation_id]
        target = step.target_object
        if step.skill_name == "search":
            raise ValueError(f"{ErrorCode.PERCEPTION_REQUIRED}: search must finish before execution")
        if not target:
            raise ValueError(f"skill {step.step_id} has no grounded target")
        if target not in objects and target != "home":
            raise ValueError(f"{ErrorCode.TARGET_NOT_FOUND}: {target}")

        if step.skill_name == "locate":
            add("locate", target, step.step_id)
        elif step.skill_name == "move":
            spatial = objects[target].get("spatial", {}) if target in objects else {}
            desired = REGION_TO_ANCHOR.get(step.semantic_target or "")
            anchors = spatial.get("anchors", {})
            if desired is not None and desired not in anchors:
                raise ValueError(f"{ErrorCode.ANCHOR_NOT_FOUND}: {target}.{desired}")
            anchor = desired if desired is not None else spatial.get("default_anchor")
            current_trace["resolved_anchor"] = anchor
            parameters: dict[str, Any] = {"planning_method": "auto"}
            if anchor:
                parameters["anchor"] = anchor
            add("move", target, step.step_id, **parameters)
        elif step.skill_name in MANIPULATION_SKILLS:
            parameters = {}
            if step.reference_object:
                parameters["reference"] = step.reference_object
            add(step.skill_name, target, step.step_id, **parameters)
        else:
            raise ValueError(f"{ErrorCode.UNSUPPORTED_SKILL}: {step.skill_name}")

        _inject_macros(add, step, operation, profile)

    command_document = CommandDocument(
        robot=robot,
        scene=scene,
        registry=str(registry_path),
        scene_fingerprint=fingerprint,
        runtime=ExecutionOptions(),
        request_defaults=registry.get("move_defaults", {}),
        commands=commands,
    )
    commands_path = output / "commands.json"
    commands_path.write_text(command_document.model_dump_json(indent=2), encoding="utf-8")
    bundle = ExecutionBundle(
        robot=robot,
        route=effective_route,
        task_dir=str(output),
        scene=scene,
        scene_fingerprint=fingerprint,
        interaction_registry=str(registry_path),
        commands=str(commands_path),
    )
    (output / "execution_bundle.json").write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    (output / "compiled_step_trace.json").write_text(
        json.dumps(traces, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "scene_plan.json").write_text(
        json.dumps(
            scene_plan.canonical_payload(base_scene_digest=_base_digest(scene_plan)),
            ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return bundle


def compile_directory(
    task_dir: str | Path,
    *,
    scene_plan: ScenePlan,
    authored_registry: Mapping[str, Any] | str | Path | None = None,
    profile_name: str = "default",
) -> ExecutionBundle:
    """Compile the planner artifacts already persisted in a task directory."""
    directory = Path(task_dir).expanduser().resolve()
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    route = str(summary.get("route") or scene_plan.route)
    authored = authored_registry if authored_registry is not None else summary.get("interaction_registry")
    if route == "B" and authored is None:
        raise ValueError(
            f"{ErrorCode.EXECUTION_METADATA_MISSING}: provide an authored interaction registry"
        )
    robot = scene_plan.robot
    registry_file = directory / "scene_registry.json"
    if registry_file.is_file():
        robot = str(json.loads(registry_file.read_text(encoding="utf-8"))["robot"])
    return compile_execution_bundle(
        load_skill_plan(directory / "skill_plan.json"),
        load_grounded_task(directory / "grounded_task.json"),
        scene_plan=scene_plan,
        output_dir=directory,
        route=route,
        authored_registry=authored,
        robot=robot,
        profile_name=profile_name,
    )


def _inject_macros(add, step, operation, profile: ExecutionProfile) -> None:
    """Post-grasp lift and post-release retreat, straight from the profile.

    These micro-steps are deterministic profile macros: their distances never
    come from the planner or the model. Relative tool moves are disabled by
    default; home macros are independent of that switch.
    """
    task_type = operation.task_type
    if step.skill_name == "grasp" and task_type == "pick_and_place":
        if not profile.disable_end_effector:
            add(
                "move",
                "end_effector",
                step.step_id,
                relation=profile.lift_relation,
                distance_m=profile.post_grasp_lift_m,
                frame=profile.lift_frame,
                path_type="linear",
            )
        add("move", "home", step.step_id)
    if step.skill_name == "release":
        if task_type == "pick_and_place":
            if not profile.disable_end_effector:
                add(
                    "move",
                    "end_effector",
                    step.step_id,
                    relation=profile.retreat_relation,
                    distance_m=profile.post_release_retreat_m,
                    frame=profile.retreat_frame,
                    path_type="linear",
                )
            add("move", "home", step.step_id)
        elif task_type in {"open", "close"}:
            add("move", "home", step.step_id)


def _interaction_registry(
    scene_plan: ScenePlan,
    route: str,
    authored: Mapping[str, Any] | str | Path | None,
    *,
    scene: str,
    fingerprint: str,
) -> dict[str, Any]:
    """Bind an authored registry, else generate metadata from the plan's measured geometry."""
    document = _load_authored(authored)
    if document is not None:
        return bind_authored_registry(document, scene=scene, scene_fingerprint=fingerprint)
    if not scene_plan.placements:
        # What decides this is whether the objects were measured, not which route produced
        # them. Route A composed its objects, and a plan read back from a live scene measures
        # them, so both can be described. Only an opaque scene -- names without geometry --
        # needs an authored file.
        raise ValueError(
            f"{ErrorCode.EXECUTION_METADATA_MISSING}: route {route} has no measured object "
            "geometry and requires an authored interaction registry"
        )
    return build_generated_registry(scene_plan, scene_fingerprint=fingerprint, scene=scene)


def _load_authored(authored: Mapping[str, Any] | str | Path | None) -> Mapping[str, Any] | None:
    if authored is None:
        return None
    if isinstance(authored, Mapping):
        return authored
    path = Path(authored).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{ErrorCode.EXECUTION_METADATA_MISSING}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _base_digest(scene_plan: ScenePlan) -> str:
    from .fingerprint import base_scene_sha256

    return base_scene_sha256(scene_plan.base_scene)


def _as_skill_plan(value: Any) -> CompilerSkillPlan:
    if isinstance(value, CompilerSkillPlan):
        return value
    if isinstance(value, Mapping):
        return skill_plan_from_mapping(value)
    if hasattr(value, "model_dump"):
        return skill_plan_from_mapping(value.model_dump(mode="json"))
    return CompilerSkillPlan(steps=tuple(value.steps), task_types=tuple(getattr(value, "task_types", ()) or ()))


def _as_grounded_task(value: Any) -> CompilerGroundedTask:
    if isinstance(value, CompilerGroundedTask):
        return value
    if isinstance(value, Mapping):
        return grounded_task_from_mapping(value)
    if hasattr(value, "model_dump"):
        return grounded_task_from_mapping(value.model_dump(mode="json"))
    return CompilerGroundedTask(operations=tuple(value.operations))
