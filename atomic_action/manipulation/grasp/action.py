"""Single primitive grasp operation."""
from __future__ import annotations
from typing import Any, Mapping

class GraspAction:
    """Open, close, and retain the gripper without post-action validation."""
    def __init__(self, config: Mapping[str, Any] | None = None, *, robot_runtime: Any = None) -> None:
        self.config = config or {}; self.robot_runtime = robot_runtime
    def execute(self, request: Mapping[str, Any]):
        runtime = self.robot_runtime
        if runtime is None: raise RuntimeError('A robot runtime is required.')
        controller = getattr(runtime, 'gripper_controller', None)
        if controller is None:
            controller = runtime.create_gripper_controller(); runtime.gripper_controller = controller
        target = request.get('target', {}).get('object_id'); grasp = request.get('grasp', {})
        params = request.get('strategy_params', {}); mode = grasp.get('mode', 'binding')
        operation = grasp.get('operation', 'grasp')
        timeout = float(request.get('constraints', {}).get('timeout', 30.0))
        if operation == 'grasp':
            controller.open(float(params.get('open_width', .08)), speed=float(params.get('close_speed', .5)), timeout=timeout)
        if target is None:
            if operation != 'verify_only':
                controller.close(float(params.get('close_width', 0.0)), speed=float(params.get('close_speed', .5)),
                                 force=float(params.get('grasp_force', 20.0)), timeout=timeout,
                                 target_object_id=None, mode='physical')
                if request.get('constraints', {}).get('hold_on_success', True):
                    controller.hold(float(params.get('hold_force', 10.0)))
            return {'success': True, 'status': 'completed',
                    'selection': {'grasp_strategy': 'default_grasp'},
                    'grasp_result': {'holding': operation != 'verify_only', 'mode': 'physical'},
                    'error': None}
        if operation != 'verify_only':
            controller.close(float(params.get('close_width', 0.0)), speed=float(params.get('close_speed', .5)), force=float(params.get('grasp_force', 20.0)), timeout=timeout, target_object_id=target, mode=mode, maximum_force=float(request.get('constraints', {}).get('maximum_force', 40.0)))
            if request.get('constraints', {}).get('hold_on_success', True): controller.hold(float(params.get('hold_force', 10.0)))
            if mode == 'binding': controller.bind(target)
            else: controller.retain_physical(target)
        return {'success': True, 'status': 'completed', 'selection': {'grasp_strategy': 'default_grasp'}, 'grasp_result': {'holding': operation != 'verify_only', 'mode': mode}, 'error': None}
