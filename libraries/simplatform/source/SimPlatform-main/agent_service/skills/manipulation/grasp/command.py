"""Compiled-command adapter for the Grasp skill."""

import json
from pathlib import Path

from agent_service.skills.command_registry import CommandExecutionContext, SkillCommandFailure, completed

GraspSkill = None
CONTACT_CONFIG_PATH = Path(__file__).with_name("contact_config.json")


def _post_contact_steps() -> int:
    """Reload the user-facing singular key; the controller uses the plural form."""
    try:
        config = json.loads(CONTACT_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        value = config["post_contact_step"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
            raise ValueError("post_contact_step must be an integer between 0 and 10000")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SkillCommandFailure({"error_code": "INVALID_CONFIG",
                                   "error_message": f"Invalid grasp config {CONTACT_CONFIG_PATH}: {exc}"}) from exc


def execute(context: CommandExecutionContext) -> dict:
    if context.skill_name != "grasp":
        raise ValueError(f"Grasp adapter cannot execute {context.skill_name!r}")
    if not hasattr(context.services.gripper, "get_state"):
        return context.invoke()
    skill_type = GraspSkill
    if skill_type is None:
        from .skill import GraspSkill as skill_type
    params = context.parameters
    request = {
        "target": {"type": "object", "object_id": context.target},
        "grasp": {
            "strategy": params.get("strategy", "auto"),
            "operation": params.get("operation", "grasp"),
            "verification_mode": params.get("verification_mode", "auto"),
        },
        "constraints": {
            "verify_grasp": params.get("verify_grasp", True),
            "retry_count": params.get("retry_count", 0),
            "hold_on_success": params.get("hold_on_success", True),
            "timeout": params.get("timeout", 10.0),
            "contact_required": params.get("contact_required", True),
            "slip_check": params.get("slip_check", True),
        },
        "strategy_params": {
            "post_contact_steps": _post_contact_steps(),
            **{
                key: params[key] for key in (
                    "open_width", "close_width", "close_speed", "grasp_force",
                    "hold_force", "settle_time", "post_contact_steps") if key in params
            },
        },
    }
    return completed(skill_type(robot_runtime=context.services.runtime).execute(request),
                     "grasp_result")
