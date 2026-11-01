"""Apply a planning margin while a payload is retained by the gripper."""


import math
from copy import deepcopy


def split_carrying_safety(request):
    """Consume the demo planning option without extending the grasp skill API."""
    result = deepcopy(request)
    value = result.get('strategy_params', {}).pop('safety_distance', 0.)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('grasp_request.strategy_params.safety_distance must be a finite non-negative number in metres')
    return result, float(value)


def payload_safety_distance(requested, held_name, carrying_distance=0.):
    """Positive-margin planning expands payload/environment clearance only.

    Zero-margin execution checks retain their actual-contact semantics.
    """
    requested = float(requested)
    return max(requested, carrying_distance) if held_name and requested > 0 else requested
