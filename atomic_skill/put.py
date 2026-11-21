"""Demo put: approach from world +Z, descend, release, and retreat."""
import numpy as np
from scipy.spatial.transform import Rotation

from atomic_skill.pick import DemoInteractionPick
from demo.interaction.command_converter import _deep_merge


def validate_release_request(request):
    """Normalize the small release request used by the demo put skill."""
    if not isinstance(request, dict) or not isinstance(request.get('target'), dict):
        raise ValueError('release request requires an object target')
    request.setdefault('release', {})
    if not isinstance(request['release'], dict):
        raise ValueError('release must be an object')
    request['release'].setdefault('operation', 'release')
    if request['release']['operation'] not in {'release', 'verify_only'}:
        raise ValueError('release.operation must be release or verify_only')
    request.setdefault('strategy_params', {})
    return request


class PutTargetError(ValueError):
    def __init__(self, held_name):
        super().__init__(f'Cannot place {held_name} onto itself')
        self.details = {
            'error_code': 'PUT_TARGET_IS_HELD_OBJECT',
            'failed_stage': 'target_resolution',
            'error_message': str(self),
            'held_object': held_name,
            'destination': held_name,
        }


def validate_put_destination(release_request, destination_name):
    if release_request['target']['object_id'] == destination_name:
        raise PutTargetError(destination_name)


def put_release_request(parameters, controller):
    """Build a release request without requiring a prevalidated grasp."""
    held_name = getattr(controller, 'held_name', None)
    request = _deep_merge({'target': {'type': 'object',
                                      'object_id': held_name or '__current_grasp__'}},
                          parameters.get('release_request', {}))
    request = validate_release_request(request)
    if request['release']['operation'] != 'release':
        raise ValueError('put requires release.operation=release')
    return request


class DemoInteractionPut(DemoInteractionPick):
    def prepare(self, request, state):
        base, resolved, goal, _, requested_path = self._resolve_request(
            request, state, prefer_direct=False, allow_fixed_orientation=True)
        # The shared search subtracts direction * distance from the goal.
        parts, resolved = self._prepare_approach(base, resolved, goal, requested_path, state,
                                               np.array([0., 0., -1.]), skill='put')
        prepend_place_reapproach(parts, resolved)
        return parts, resolved


def prepend_place_reapproach(parts, resolved):
    """A changed placement XY needs a fresh approach before contact/release."""
    recovery = resolved.get('placement_revalidation') or {}
    previous = recovery.get('previous_world_from_control')
    if previous is None:
        return
    goal = np.asarray(resolved['selected']['world_from_control'], dtype=float)
    old = np.asarray(previous, dtype=float)
    if np.linalg.norm(goal[:2, 3] - old[:2, 3]) <= 1.e-6:
        return
    parts.insert(0, {
        'phase': 'placement_reapproach',
        'request': {},
        'parameters': {
            'target_pose': {'position': goal[:3, 3].tolist(),
                            'quaternion_xyzw': Rotation.from_matrix(goal[:3, :3]).as_quat().tolist()},
            'grasp_offset_local': np.asarray(resolved['control_from_grasp'])[:3, 3].tolist(),
        },
    })
    resolved['placement_reapproach'] = {
        'reason': 'placement_xy_changed',
        'previous_world_from_control': old.tolist(),
        'world_from_control': goal.tolist(),
        'xy_shift_m': float(np.linalg.norm(goal[:2, 3] - old[:2, 3])),
    }



