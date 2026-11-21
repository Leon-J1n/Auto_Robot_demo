"""Apply a planning margin while a payload is retained by the gripper."""


import math
from copy import deepcopy

DEFAULT_CARRYING_SAFETY_DISTANCE = -0.03


def split_carrying_safety(request):
    """Consume the demo planning option without extending the grasp skill API."""
    result = deepcopy(request)
    value = result.get('strategy_params', {}).pop('safety_distance', DEFAULT_CARRYING_SAFETY_DISTANCE)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('grasp_request.strategy_params.safety_distance must be a finite number in metres')
    return result, float(value)


def payload_safety_distance(requested, held_name, carrying_distance=DEFAULT_CARRYING_SAFETY_DISTANCE):
    """Positive-margin planning applies signed payload/environment clearance.

    Zero-margin execution checks retain their actual-contact semantics.
    """
    requested = float(requested)
    if held_name and requested > 0:
        return float(carrying_distance) if carrying_distance < 0 else max(requested, carrying_distance)
    return requested


def payload_pair_limit(requested, carrying_distance, first, second, payload_bodies, robot_bodies, *, support=False):
    """Apply carrying clearance only between the payload and external world."""
    limit = -.001 if support else float(requested)
    external = ((first in payload_bodies and second not in payload_bodies and second not in robot_bodies)
                or (second in payload_bodies and first not in payload_bodies and first not in robot_bodies))
    if external and requested > 0:
        payload_limit = payload_safety_distance(requested, True, carrying_distance)
        return min(limit, payload_limit) if support else payload_limit
    return limit
