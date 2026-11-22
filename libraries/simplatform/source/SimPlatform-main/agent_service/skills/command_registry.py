"""Resolve compiled runtime commands through the restored skill tree."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import logging
from types import ModuleType
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


class SkillCommandFailure(RuntimeError):
    def __init__(self, error: Mapping[str, Any]) -> None:
        super().__init__(str(error.get("error_message", "Skill execution failed.")))
        self.error_code = str(error.get("error_code", "EXECUTION_FAILED"))
        self.recoverable = bool(error.get("recoverable", False))
        self.details = error.get("details")


@dataclass(frozen=True)
class CommandExecutionContext:
    """Runtime-owned state exposed to one skill command adapter."""

    skill_name: str
    services: Any
    command: Any
    frames: Any
    outcome: Any
    motions_remaining: int
    invoke_runtime: Callable[["CommandExecutionContext"], dict[str, Any]]

    def invoke(self) -> dict[str, Any]:
        return self.invoke_runtime(self)

    @property
    def parameters(self) -> Mapping[str, Any]:
        if isinstance(self.command, Mapping):
            value = self.command.get("parameters", {})
        else:
            value = getattr(self.command, "parameters", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def target(self) -> str:
        return str(self.parameters.get("target", "") or "")


def completed(result: Mapping[str, Any], field: str) -> dict[str, Any]:
    if not result.get("success"):
        raise SkillCommandFailure(result.get("error") or {})
    payload = dict(result.get(field) or {})
    payload["selection"] = dict(result.get("selection") or {})
    return payload


class SkillCommandRegistry:
    """Map command skill names to adapters colocated with their skill packages."""

    def __init__(self, routes: Mapping[str, str]) -> None:
        self._routes = dict(routes)
        if not self._routes or any(not name or not module for name, module in self._routes.items()):
            raise ValueError("skill command routes must have non-empty names and modules")

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._routes)

    def module_for(self, skill_name: str) -> str:
        try:
            return self._routes[skill_name]
        except KeyError as exc:
            raise KeyError(f"no command adapter registered for skill {skill_name!r}") from exc

    def execute(self, context: CommandExecutionContext) -> dict[str, Any]:
        module = self._load(context.skill_name)
        execute = getattr(module, "execute", None)
        if not callable(execute):
            raise TypeError(f"{module.__name__} must expose execute(context)")
        logger.debug("Dispatching command through skill adapter", extra={
            "skill_name": context.skill_name, "adapter_module": module.__name__})
        result = execute(context)
        if not isinstance(result, dict):
            raise TypeError(f"{module.__name__}.execute() must return a dict")
        return result

    def _load(self, skill_name: str) -> ModuleType:
        return import_module(self.module_for(skill_name))


COMMAND_SKILLS = SkillCommandRegistry({
    "locate": "agent_service.skills.perception.location.command",
    "move": "agent_service.skills.motion.move.command",
    "grasp": "agent_service.skills.manipulation.grasp.command",
    "release": "agent_service.skills.manipulation.release.command",
    "press": "agent_service.skills.manipulation.press.command",
    "push": "agent_service.skills.manipulation.push_pull.command",
    "pull": "agent_service.skills.manipulation.push_pull.command",
})
