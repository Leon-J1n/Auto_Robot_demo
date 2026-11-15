"""cuRobo planner adapter."""

from .planner import CuroboPlanner, MoveCuroboAction

# Keep the protocol-facing name as a compatibility alias while exposing the
# normalized planner name to new callers.
MoveCuroboSkill = CuroboPlanner

__all__ = ['CuroboPlanner', 'MoveCuroboAction', 'MoveCuroboSkill']

