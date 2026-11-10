"""Single primitive physical grasp operation."""
from __future__ import annotations

from typing import Any, Mapping

DEFAULT_OPEN_WIDTH_M = .08


class GraspAction:
    """Close the gripper and let measured contact determine whether it holds."""

    def __init__(self, config: Mapping[str, Any] | None = None, *, robot_runtime: Any = None) -> None:
        self.config = config or {}
        self.robot_runtime = robot_runtime

    def execute(self, request: Mapping[str, Any]):
        runtime = self.robot_runtime
        if runtime is None:
            raise RuntimeError('A robot runtime is required.')
        controller = getattr(runtime, 'gripper_controller', None)
        if controller is None:
            controller = runtime.create_gripper_controller()
            runtime.gripper_controller = controller

        grasp = request.get('grasp', {})
        params = request.get('strategy_params', {})
        constraints = request.get('constraints', {})
        operation = grasp.get('operation', 'grasp')
        if operation == 'verify_only':
            state = controller.get_state()
            holding = bool(state.get('holding'))
            return {'success': holding, 'status': 'completed' if holding else 'failed',
                    'selection': {'grasp_strategy': 'default_grasp'},
                    'grasp_result': {'holding': holding, 'mode': 'physical', 'state': state},
                    'error': None if holding else {'error_code': 'GRASP_NOT_CONFIRMED',
                                                   'error_message': 'No physical grasp is confirmed.'}}

        timeout = float(constraints.get('timeout', 30.0))
        controller.open(float(params.get('open_width', DEFAULT_OPEN_WIDTH_M)),
                        speed=float(params.get('close_speed', .5)), timeout=timeout)
        result = controller.close(
            float(params.get('close_width', 0.0)),
            speed=float(params.get('close_speed', .5)),
            force=float(params.get('grasp_force', 20.0)),
            timeout=timeout,
            maximum_force=float(constraints.get('maximum_force', 40.0)),
        )
        holding = bool(result.get('contact_detected'))
        if holding and constraints.get('hold_on_success', True):
            controller.hold(float(params.get('hold_force', 10.0)))
        evidence = result.get('evidence', {})
        reason = evidence.get('reason')
        if reason == 'no_contact_force':
            message = '夹爪已闭合到位，但未检测到有效接触力，未抓到物体'
        elif reason == 'closure_timeout_no_contact_force':
            message = '闭爪超时，未检测到有效接触力，未确认抓到物体'
        elif reason == 'force_limit_exceeded':
            message = '闭爪接触力超过允许上限，抓取失败'
        else:
            message = '闭爪后未形成稳定的双侧接触力，未确认抓到物体'
        return {'success': holding, 'status': 'completed' if holding else 'failed',
                'selection': {'grasp_strategy': 'default_grasp'},
                'grasp_result': {'holding': holding, 'mode': 'physical', **result},
                'error': None if holding else {'error_code': 'GRASP_NOT_CONFIRMED',
                                               'failed_stage': 'grasp',
                                               'error_message': message,
                                               'details': {'confirmation': dict(evidence)}}}
