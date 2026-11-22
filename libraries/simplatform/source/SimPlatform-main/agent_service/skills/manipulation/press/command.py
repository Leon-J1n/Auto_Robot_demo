"""Compiled-command adapter for the Press skill."""

from agent_service.skills.command_registry import CommandExecutionContext, completed

PressSkill = None


def execute(context: CommandExecutionContext) -> dict:
    if context.skill_name != "press":
        raise ValueError(f"Press adapter cannot execute {context.skill_name!r}")
    if not hasattr(context.services.runtime, "qpos_indices"):
        return context.invoke()
    skill_type = PressSkill
    if skill_type is None:
        from .skill import PressSkill as skill_type
    params = context.parameters
    direction = params.get("direction", [0.0, 0.0, -1.0])
    control_mode = str(params.get("control_mode", "displacement"))
    strategy_params = dict(params.get("strategy_params", {}))
    if control_mode == "force" and "target_force" in params:
        strategy_params.setdefault("target_force", params["target_force"])
    if control_mode != "force":
        strategy_params.setdefault(
            "press_depth", params.get("press_depth", params.get("maximum_travel", 0.03)))
    request = {
        "target": {"type": "button", "object_id": context.target,
                   "press_direction": direction},
        "press": {"strategy": params.get("strategy", "auto"),
                  "control_mode": control_mode,
                  "retract_after_press": params.get("retract_after_press", True)},
        "constraints": {
            key: params[key] for key in (
                "maximum_force", "maximum_travel", "contact_threshold", "press_speed",
                "hold_time", "verify_press", "retry_count", "timeout", "require_contact")
            if key in params
        },
        "strategy_params": strategy_params,
    }
    return completed(skill_type(robot_runtime=context.services.runtime).execute(request),
                     "press_result")
