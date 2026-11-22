"""Compiled-command adapter for the Locate skill."""

from agent_service.skills.command_registry import CommandExecutionContext


def execute(context: CommandExecutionContext) -> dict:
    if context.skill_name != "locate":
        raise ValueError(f"Locate adapter cannot execute {context.skill_name!r}")
    return context.invoke()
