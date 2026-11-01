"""Demo put: approach from world +Z, descend, release, and retreat."""
import numpy as np

from demo.task_skills.pick import DemoInteractionPick
from demo.task_commands.converter import _deep_merge
from skills.manipulation.release.utils.validation import validate_release_request


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
    """Resolve the payload separately from the placement destination."""
    held_name = getattr(controller, 'held_name', None)
    if not held_name:
        raise ValueError('put requires a currently held object')
    request = _deep_merge({'target': {'type': 'object', 'object_id': held_name}},
                          parameters.get('release_request', {}))
    request = validate_release_request(request)
    if request['target']['object_id'] != held_name:
        raise ValueError('put release target must match the currently held object')
    if request['release']['operation'] != 'release':
        raise ValueError('put requires release.operation=release')
    return request


class DemoInteractionPut(DemoInteractionPick):
    def prepare(self, request, state):
        base, resolved, goal, _, requested_path = self._resolve_request(
            request, state, prefer_direct=False, allow_fixed_orientation=True)
        # The shared search subtracts direction * distance from the goal.
        return self._prepare_approach(base, resolved, goal, requested_path, state,
                                      np.array([0., 0., -1.]), skill='put')


