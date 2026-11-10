"""Single primitive release operation."""
from __future__ import annotations
from typing import Any, Mapping

class ReleaseAction:
    """Open the gripper without post-action validation."""
    def __init__(self, config: Mapping[str, Any] | None = None, *, robot_runtime: Any = None) -> None:
        self.config = config or {}; self.robot_runtime = robot_runtime
    def execute(self, request: Mapping[str, Any]):
        runtime = self.robot_runtime
        if runtime is None: raise RuntimeError('A robot runtime is required.')
        controller = getattr(runtime, 'gripper_controller', None)
        if controller is None:
            from control import MujocoGripperController
            controller = MujocoGripperController(runtime); runtime.gripper_controller = controller
        release = request.get('release', {}); params = request.get('strategy_params', {})
        if release.get('operation', 'release') != 'verify_only':
            controller.open(float(params.get('open_width', .08)), speed=float(params.get('open_speed', .5)), timeout=float(request.get('constraints', {}).get('timeout', 30.0)))
        return {'success': True, 'status': 'completed', 'selection': {'release_strategy': 'default_release'}, 'release_result': {'holding': False}, 'error': None}
