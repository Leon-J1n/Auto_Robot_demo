"""Compiled-command adapter for the PushPull skill."""

from agent_service.skills.command_registry import CommandExecutionContext, completed

PushPullSkill = None


def execute(context: CommandExecutionContext) -> dict:
    if context.skill_name not in {"push", "pull"}:
        raise ValueError(f"PushPull adapter cannot execute {context.skill_name!r}")
    if not hasattr(context.services.runtime, "qpos_indices"):
        return context.invoke()
    skill_type = PushPullSkill
    if skill_type is None:
        from .skill import PushPullSkill as skill_type
    params = context.parameters
    interaction_mode = str(params.get("interaction_mode", "free_object"))
    target_type = "mechanism" if interaction_mode == "mechanism" else "object"
    direction = list(params.get("direction", [1.0, 0.0, 0.0]))
    signed_direction = direction if context.skill_name == "push" else [-v for v in direction]
    request = {
        "target": {"type": target_type, "object_id": context.target,
                   "contact_normal": [-v for v in signed_direction]},
        "manipulation": {"operation": context.skill_name, "trajectory": "linear",
                         "interaction_mode": interaction_mode,
                         "maintain_contact": params.get("maintain_contact", True)},
        "constraints": {
            key: params[key] for key in (
                "maximum_force", "maximum_travel", "contact_threshold", "speed",
                "timeout", "verify_motion", "stop_on_contact_loss") if key in params
        },
        "strategy_params": {"direction": signed_direction,
                            "distance": params.get("distance", 0.02)},
    }
    return completed(skill_type(robot_runtime=context.services.runtime).execute(request),
                     "push_pull_result")
