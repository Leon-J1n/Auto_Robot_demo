"""Executable skill packages and command-dispatch integration."""

import sys

# Restored demo2 skills use ``skills.*`` as their public package path.  The service
# embeds that tree under ``agent_service``, so keep the original imports valid.
sys.modules.setdefault("skills", sys.modules[__name__])

from .command_registry import (COMMAND_SKILLS, CommandExecutionContext,
                               SkillCommandFailure, SkillCommandRegistry)

__all__ = ["COMMAND_SKILLS", "CommandExecutionContext", "SkillCommandFailure",
           "SkillCommandRegistry"]
