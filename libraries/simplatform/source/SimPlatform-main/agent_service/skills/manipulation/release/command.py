"""Compiled-command adapter for the Release skill."""

from agent_service.skills.command_registry import CommandExecutionContext, completed

ReleaseSkill = None


def execute(context: CommandExecutionContext) -> dict:
    if context.skill_name != "release":
        raise ValueError(f"Release adapter cannot execute {context.skill_name!r}")
    if not hasattr(context.services.gripper, "get_state"):
        return context.invoke()
    skill_type = ReleaseSkill
    if skill_type is None:
        from .skill import ReleaseSkill as skill_type
    params = context.parameters
    request = {
        "target": {"type": "object", "object_id": context.target},
        "release": {
            "strategy": params.get("strategy", "auto"),
            "operation": params.get("operation", "release"),
            "verification_mode": params.get("verification_mode", "auto"),
        },
        "constraints": {
            "verify_release": params.get("verify_release", True),
            "retry_count": params.get("retry_count", 0),
            "timeout": params.get("timeout", 5.0),
        },
        "strategy_params": {
            key: params[key] for key in ("open_width", "open_speed", "settle_time")
            if key in params
        },
    }
    return completed(skill_type(robot_runtime=context.services.runtime).execute(request),
                     "release_result")
