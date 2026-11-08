"""Demo pick: approach, straight contact, then straight retreat to the pre-point."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation

from planning import PickContactPlanner, candidate_distances
from atomic_action.motion.common.interaction import DemoInteractionMove, pose_request, grasp_pose_move_request
from atomic_skill.approach import ApproachSkill, run_candidate_search
from atomic_skill.retreat import RetreatSkill

APPROACH_DISTANCES = (.15, .12, .10, .14, .13, .11)


class ApproachResolutionError(ValueError):
    def __init__(self, skill, rejected):
        super().__init__(f'No valid {skill} pre-point or contact path: {rejected}')
        categories = {item['category'] for item in rejected}
        codes = {
            'prepoint_ik': 'PREPOINT_UNREACHABLE',
            'prepoint_collision': 'PREPOINT_COLLISION',
            'contact_ik': 'CONTACT_PATH_UNREACHABLE',
            'contact_collision': 'CONTACT_PATH_COLLISION',
            'contact_planning': 'CONTACT_PATH_PLANNING_FAILED',
        }
        self.details = {
            'error_code': codes[next(iter(categories))] if len(categories) == 1 else 'NO_VALID_APPROACH',
            'failed_stage': 'target_resolution',
            'rejected': rejected,
            'error_message': str(self),
        }


def execute_retreat_candidates(part, measured_pose, resolved, execute):
    """Try shorter/alternate distances only when planning failed before motion."""
    attempts = []
    direction = np.asarray(resolved['approach']['direction_world'])
    result = None
    def attempt(distance):
        nonlocal result
        part['translation_world'] = (-distance * direction).tolist()
        resolve_post_grasp_target(part, measured_pose, resolved)
        result = execute(part['request'])
        error = result.get('error') or {}
        attempts.append({'distance_m': distance, 'success': bool(result.get('success')),
                         'error': deepcopy(error) if error else None})
        resolved['retreat'].update(distance_m=distance, attempts=attempts)
        if result.get('success') or error.get('failed_stage') not in {'planning', 'kinematics', 'path_generation'}:
            return True if result.get('success') else False
        return None
    run_candidate_search(candidate_distances({}, APPROACH_DISTANCES, label='retreat'), attempt)
    return result


def resolve_post_grasp_target(part, measured_pose, resolved):
    """Resolve a relative segment from the measured pose immediately before planning."""
    offset = part.get('translation_world')
    if offset is None:
        return
    goal = np.eye(4)
    goal[:3, :3] = measured_pose['rotation_matrix']
    goal[:3, 3] = np.asarray(measured_pose['position'])
    norm = np.linalg.norm(offset)
    if norm > 1e-12:
        goal = ApproachSkill.candidate_pose(goal, -np.asarray(offset) / norm, norm)
    part['request']['target'] = pose_request(goal)
    resolved[part['phase']]['world_from_control'] = goal.tolist()


class DemoInteractionPick(DemoInteractionMove):
    def __init__(self, resolver, resources, kinematics, collision_checker, joint_limits):
        super().__init__(resolver, resources, kinematics, collision_checker, joint_limits)
        self.contact_planner = PickContactPlanner(
            kinematics=kinematics, collision_checker=collision_checker, joint_limits=joint_limits)

    def prepare(self, request, state, *, grasp_request=None, retreat_lift_height=0.01):
        if (isinstance(retreat_lift_height, bool) or not isinstance(retreat_lift_height, (int, float))
                or not np.isfinite(retreat_lift_height) or retreat_lift_height < 0):
            raise ValueError('retreat_lift_height must be a finite nonnegative number in metres')
        base, resolved, goal, world_grasp, requested_path = self._resolve_request(request, state, prefer_direct=False, closure_request=grasp_request)
        # Approach along the physical interaction frame, not an unrelated flange
        # axis. Moving the fixed grasp frame translates the controlled TCP equally.
        direction = world_grasp[:3, 2]
        return self._prepare_approach(base, resolved, goal, requested_path, state, direction,
                                      skill='pick', retreat_lift_height=retreat_lift_height)

    def _prepare_approach(self, base, resolved, goal, requested_path, state, direction, *, skill,
                          retreat_lift_height=0.):
        if skill in {'pick', 'put'}:
            # Picking goes directly to the selected grasp control pose. Try a
            # Cartesian move first with a relaxed per-waypoint joint step; if
            # that branch cannot be built, the runner retries the same target
            # through a direct joint move before closing the gripper.
            linear = deepcopy(base)
            linear['motion'].update(path_type='linear', path_constraint='soft', phase='contact')
            linear.setdefault('planning', {}).update(mode='direct', allow_replan=False)
            linear.setdefault('strategy_params', {}).update(
                max_joint_step=0.8, linear_search_mode='fast')
            joint = deepcopy(base)
            joint['motion'].update(path_type='joint', path_constraint='soft', phase='contact')
            # The fallback is the recovery route: let MoveJoint switch to the
            # collision-free planner when a direct joint segment is blocked.
            joint.setdefault('planning', {}).update(mode='collision_free', allow_replan=True)
            if not linear['constraints'].get('keep_end_effector_orientation'):
                joint['target'] = {'type': 'joint', 'joint_positions': resolved['joint_positions']}
            resolved['approach'] = {
                'used': False, 'mode': 'direct_grasp_move',
                'path_type': 'linear', 'fallback_path_type': 'joint',
                'distance_m': 0.0, 'direction_world': np.asarray(direction, dtype=float).tolist(),
            }
            return self._post_contact_parts(
                linear, goal, goal, 0., direction, resolved, skill,
                retreat_lift_height,
                [{'phase': 'contact', 'request': linear,
                  'direct_pick_fallback': joint}],
            )
        planned = self.contact_planner.plan(
            base, state, goal=goal, direction=direction, requested_path=requested_path,
            distances=candidate_distances({}, APPROACH_DISTANCES, label='approach'),
            pose_request=pose_request)
        if planned and planned[0] is not None:
            selected = planned[0]
            approach = selected['approach']
            distance = selected['distance']
            contact = selected['contact']
            transit = selected['transit']
            resolved['approach'] = {'used': True, 'distance_m': distance,
                'world_from_control': approach.tolist(), 'direction_world': direction.tolist(),
                'joint_positions': selected['joint_positions'].tolist(), 'rejected': selected['rejected']}
            return self._post_contact_parts(contact, goal, approach, distance, direction,
                resolved, skill, retreat_lift_height,
                [{'phase':'approach','request':transit}, {'phase':'contact','request':contact}])
        rejected = planned[1] if planned else []
        if skill == 'pick':
            # The selected grasp pose has already passed IK/collision/closure checks.
            # Only the constrained approach failed; preserve that goal for move.
            contact = grasp_pose_move_request({**base, 'target':pose_request(goal)})
            distance = APPROACH_DISTANCES[0]
            approach = ApproachSkill.candidate_pose(goal, direction, distance)
            resolved['approach'] = {'used':False, 'mode':'collision_free_to_grasp',
                'world_from_control':goal.tolist(), 'direction_world':direction.tolist(),
                'rejected':rejected}
            return self._post_contact_parts(contact, goal, approach, distance, direction,
                resolved, skill, retreat_lift_height, [{'phase':'contact','request':contact,
                'grasp_pose_fallback':{'reason':'NO_VALID_LINEAR_APPROACH','rejected':deepcopy(rejected)}}])
        raise ApproachResolutionError(skill, rejected)

    def _post_contact_parts(self, contact, goal, approach, distance, direction,
                            resolved, skill, retreat_lift_height, parts):
        # Pick is intentionally a two-step atomic composition: reach the
        # selected grasp pose, then execute the grasp action.  It does not
        # perform a lift or retreat; those are separate responsibilities.
        if skill in {'pick', 'put'}:
            return parts, resolved
        retreat = deepcopy(contact)
        retreat['motion'].update(path_type='linear',path_constraint='hard')
        retreat['constraints']['keep_end_effector_orientation'] = True
        retreat['planning'].update(mode='direct',allow_replan=False)
        retreat_pose = RetreatSkill.candidate_pose(approach, [0., 0., -1.], retreat_lift_height)
        resolved['retreat'] = {'world_from_control': retreat_pose.tolist(),
                               'lift_height_m': retreat_lift_height, 'lift_frame': 'world'}
        retreat['target'] = pose_request(retreat_pose)
        retreat['motion']['phase'] = 'retreat'
        if skill == 'pick' and retreat_lift_height > 0:
            lift = deepcopy(retreat)
            lift_pose = goal.copy()
            lift_pose[2, 3] += retreat_lift_height
            lift['target'] = pose_request(lift_pose)
            resolved['lift'] = {'world_from_control': lift_pose.tolist(),
                                'height_m': retreat_lift_height, 'frame': 'world'}
            parts.append({'phase': 'lift', 'request': lift,
                          'translation_world': [0., 0., retreat_lift_height]})
        retreat_part = {'phase': 'retreat', 'request': retreat}
        if skill == 'pick':
            retreat_part['translation_world'] = (-distance * direction).tolist()
        parts.append(retreat_part)
        return parts, resolved


