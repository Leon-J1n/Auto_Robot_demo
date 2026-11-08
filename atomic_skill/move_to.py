"""Public move_to atomic skill.

The command layer resolves the target and optional relative relation into a
pose request.  Execution reuses the existing move action through
``DemoInteractionMove``; the public ``move`` task name is intentionally not
exposed.
"""

from atomic_action.motion.common.interaction import DemoInteractionMove


class MoveToSkill(DemoInteractionMove):
    """Move to an absolute or relation-offset target pose."""



