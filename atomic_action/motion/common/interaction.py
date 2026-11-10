"""Shared motion request and interaction execution helpers."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from atomic_action.motion.common.utils.validation import validate_move_request


def pose_request(transform):
    return {'type': 'pose', 'frame': 'world',
        'position': dict(zip('xyz', transform[:3, 3].tolist())),
        'orientation': dict(representation='quaternion',
                            **dict(zip('xyzw', Rotation.from_matrix(transform[:3, :3]).as_quat().tolist())))}


def grasp_pose_move_request(request):
    """Relax only the route to an already selected world-frame grasp pose."""
    result = deepcopy(request)
    if result.get('target', {}).get('type') != 'pose' or result['target'].get('frame', 'world') != 'world':
        raise ValueError('Grasp approach fallback requires a resolved world-frame pose')
    result['motion'] = {'path_type':'joint', 'path_constraint':'soft', 'phase':'contact'}
    result.setdefault('constraints', {}).update(avoid_collision=True, keep_end_effector_orientation=False)
    planning = result.setdefault('planning', {})
    planning.update(mode='collision_free', allow_replan=True,
                    planning_time=max(10., float(planning.get('planning_time', 10.))))
    return result


def planning_failed_without_motion(result, backend, physics_steps_before):
    """Retry planning only; execution failures or any live steps prohibit fallback."""
    return (not result.get('success') and
            (result.get('error') or {}).get('failed_stage') in {'planning', 'kinematics', 'path_generation'} and
            not result.get('motion_executed') and
            backend.physics_steps == physics_steps_before)


def execute_grasp_pose_move(request, execute_motion, backend):
    """Use ordinary move execution, then measure the endpoint before grasping."""
    request = grasp_pose_move_request(request)
    target = request['target']
    orientation = target['orientation']
    expected = np.asarray([target['position'][key] for key in 'xyz'], dtype=float)
    quaternion = tuple(float(orientation[key]) for key in 'xyzw')
    rotation = Rotation.from_quat(quaternion).as_matrix()
    # Limit the existing pad/handle contact exception to the terminal pose area.
    previous = getattr(backend, 'grasp_approach_goal', None)
    backend.grasp_approach_goal = (tuple(expected), quaternion)
    try:
        result = execute_motion(request)
    finally:
        backend.grasp_approach_goal = previous
    if result.get('success'):
        measured = backend.get_end_effector_pose()
        position_error = float(np.linalg.norm(np.asarray(measured['position'])-expected))
        angle_error = float(Rotation.from_matrix(rotation.T @ measured['rotation_matrix']).magnitude())
        verified = position_error <= .002 and angle_error <= .02
        result = {**result, 'grasp_pose_verification':{
            'verified':verified, 'position_error_m':position_error, 'orientation_error_rad':angle_error}}
        if not verified:
            result.update(success=False, status='failed', error={
                'error_code':'POSE_VERIFICATION_FAILED', 'failed_stage':'interaction_position_verification',
                'error_message':'Avoidance move did not reach the selected grasp pose'})
    return request, result


class DemoInteractionMove:
    """Produce ordinary _MoveExecutor requests, never extend the skill's interface."""

    def __init__(self, resolver, resources, kinematics, collision_checker, joint_limits):
        self.resolver, self.resources = resolver, resources
        self.kinematics, self.checker = kinematics, collision_checker

    def _resolve_request(self, request, state, *, prefer_direct, closure_request=None,
                         allow_fixed_orientation=False):
        target = request['target']
        hold_orientation = request.get('constraints', {}).get('keep_end_effector_orientation', False)
        if hold_orientation and not allow_fixed_orientation:
            raise ValueError('Pick targets require their service grasp orientation; fixed orientation is supported for move only')
        if request.get('target_body_name'):
            raise ValueError('Interaction moves cannot ignore target collisions')
        self.checker.allowed_target_geom_ids = None
        if hasattr(self.checker, 'set_allowed_target_names'):
            self.checker.set_allowed_target_names(set())
        if hasattr(self.checker, 'allowed_proxy_target_names'):
            self.checker.allowed_proxy_target_names = set()
        resolved = self.resolver.resolve(target['target_id'], self.resources, state)
        # The selected grasp pose is expected to bring the gripper into the
        # target; keep this narrow proxy allowance for candidate selection.
        if closure_request is not None and hasattr(self.checker, 'allowed_proxy_target_names'):
            self.checker.allowed_proxy_target_names = {str(resolved['target_name'])}
        if hold_orientation:
            resolved = deepcopy(resolved)
            rotation = np.asarray(self.kinematics.forward(state['joints'])['rotation_matrix'])
            tool = np.asarray(resolved['control_from_grasp'])
            for candidate in resolved['candidates']:
                grasp = np.asarray(candidate['world_from_grasp']).copy()
                candidate['service_world_from_grasp'] = grasp.tolist()
                # Preserve the service interaction point, not the flange position
                # derived using a service orientation that is no longer requested.
                grasp[:3, :3] = rotation @ tool[:3, :3]
                candidate['world_from_grasp'] = grasp.tolist()
                candidate['world_from_control'] = (grasp @ np.linalg.inv(tool)).tolist()
            resolved['orientation_source'] = 'current_control_pose'
        # Validate the ordinary request before doing expensive candidate queries.
        base = deepcopy(request)
        base['target'] = pose_request(np.asarray(resolved['candidates'][0]['world_from_control']))
        base = validate_move_request(base)
        validator = None
        if closure_request is not None and not resolved.get('depth_search_pending'):
            # A pending VLM depth search performs this static check itself.
            from demo.interaction.table_clearance import table_floor, validate_grasp_geometry
            def validator(q, candidate):
                point = np.asarray(candidate['world_from_grasp'])[:3, 3]
                floor = table_floor(self.checker.backend, point)
                return validate_grasp_geometry(self.checker, q, floor,
                    safety_distance=base['constraints']['safety_distance'])
        resolved = self.resolver.select(resolved, self.kinematics, self.checker, state['joints'],
            safety_distance=base['constraints']['safety_distance'], grasp_index=target.get('grasp_index'),
            prefer_direct=prefer_direct, candidate_validator=validator)
        remember = getattr(self.resolver, 'remember_selection', None)
        if remember is not None:
            remember(resolved)
        goal = np.asarray(resolved['selected']['world_from_control'])
        world_grasp = np.asarray(resolved['selected']['world_from_grasp'])
        base['target'] = pose_request(goal)
        requested_path = base['motion']['path_type']
        if requested_path == 'auto':
            requested_path = 'joint'
        base['motion']['path_type'] = requested_path
        return base, resolved, goal, world_grasp, requested_path

    def prepare(self, request, state):
        # Rank goals by joint travel. Let the motion planner detour to a near
        # goal instead of replacing it with a distant directly reachable goal.
        base, resolved, _, _, path_type = self._resolve_request(
            request, state, prefer_direct=False, allow_fixed_orientation=True)
        resolved['approach'] = {'used': False, 'reason': 'single_target_move'}
        if path_type == 'joint' and not base['constraints'].get('keep_end_effector_orientation'):
            base['target'] = {'type': 'joint', 'joint_positions': resolved['joint_positions']}
        return [{'phase': 'move', 'request': base}], resolved


