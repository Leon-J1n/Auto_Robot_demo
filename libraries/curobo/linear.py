"""Cartesian targets and independent geometric checks for cuRobo linear moves."""
from __future__ import annotations

import time

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .conversion.scene import goal_in_base


class LinearCandidateRejected(ValueError):
    pass


class LinearBranchNotFound(RuntimeError):
    pass


def offset_world_target(target, offset):
    delta = np.asarray(offset, dtype=float)
    if target.get('frame') != 'world' or delta.shape != (3,) or not np.isfinite(delta).all():
        raise ValueError('Batched linear targets require a world pose and finite XYZ offsets')
    position = target['position']
    if isinstance(position, dict):
        position = [position[axis] for axis in 'xyz']
    return {**target, 'position': dict(zip('xyz', (np.asarray(position) + delta).tolist()))}


def search_linear_targets(indices, solve_endpoints, plan_target, *,
                          deadline, batch_size=4, diagnostics):
    """Screen endpoints in groups and plan only one feasible target at a time."""
    diagnostics.update(endpoint_ik_batches=0, endpoints_tested=0, waypoint_searches=[])
    last_error = None
    for first in range(0, len(indices), batch_size):
        if time.monotonic() >= deadline:
            raise TimeoutError('cuRobo linear endpoint screening exceeded planning time')
        group = indices[first:first + batch_size]
        results = solve_endpoints(group)
        diagnostics['endpoint_ik_batches'] += 1
        diagnostics['endpoints_tested'] += len(group)
        for index, solutions in zip(group, results, strict=True):
            if not np.isfinite(solutions).all(axis=1).any():
                continue
            stats = {'index': index}
            diagnostics['waypoint_searches'].append(stats)
            try:
                return index, plan_target(index, stats)
            except LinearBranchNotFound as error:
                last_error = error
    if last_error is not None:
        raise last_error
    return None


def search_ik_branches(initial, waypoint_count, solve_candidates, validate_path, *,
                       max_joint_step=0.4, max_paths=32, max_ik_calls=128, deadline=None,
                       diagnostics=None, solve_batch=None, batch_size=4):
    joints = [np.asarray(initial, dtype=float).copy()]
    cache = {}
    attempts = 0
    ik_calls = 0
    ik_batches = 0
    last_rejection = None
    timed_out = False
    ik_budget_exhausted = False
    blocked = {}

    def candidates_for(index, parents):
        nonlocal ik_calls, ik_batches, ik_budget_exhausted
        pending = {}
        for parent in parents:
            key = (index, parent.tobytes())
            if key not in cache:
                pending.setdefault(key, parent)
        if not pending:
            return
        remaining = max_ik_calls - ik_calls
        if remaining == 0:
            ik_budget_exhausted = True
            return
        pending = list(pending.items())[:min(batch_size, remaining)]
        seeds = [parent for _, parent in pending]
        if solve_batch is not None and len(seeds) > 1:
            results = solve_batch(index, np.stack(seeds))
            ik_batches += 1
        else:
            results = [solve_candidates(index, parent) for parent in seeds]
            ik_batches += len(seeds)
        if len(results) != len(seeds):
            raise ValueError('cuRobo IK returned an invalid number of branch results')
        ik_calls += len(seeds)
        for ((key, parent), candidates) in zip(pending, results):
            candidates = np.asarray(candidates, dtype=float)
            if candidates.ndim != 2 or candidates.shape[1] != parent.size:
                raise ValueError('cuRobo IK returned invalid candidate dimensions')
            cache[key] = candidates

    def explore(index, alternatives=()):
        nonlocal attempts, last_rejection, timed_out
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            return None
        if index == waypoint_count:
            if attempts >= max_paths:
                return None
            attempts += 1
            try:
                return validate_path(np.stack(joints))
            except LinearCandidateRejected as error:
                last_rejection = error
                return None
        key = (index, joints[-1].tobytes())
        if key not in cache:
            candidates_for(index, (joints[-1], *alternatives))
            if key not in cache:
                return None
        candidates = cache[key]
        distances = np.linalg.norm(candidates - joints[-1], axis=1)
        jumps = np.max(np.abs(candidates - joints[-1]), axis=1) if len(candidates) else np.array([])
        usable = np.isfinite(jumps) & (jumps <= max_joint_step)
        if not usable.any():
            minimum = float(np.min(jumps[np.isfinite(jumps)])) if np.isfinite(jumps).any() else None
            previous = blocked.get(index)
            if previous is None or (minimum is not None and
                                    (previous['minimum_joint_step'] is None or minimum < previous['minimum_joint_step'])):
                nearest = candidates[int(np.argmin(jumps))] if minimum is not None else None
                blocked[index] = {'waypoint': index + 1, 'successful_ik_candidates': len(candidates),
                                  'minimum_joint_step': minimum,
                                  'previous': joints[-1].tolist(),
                                  'nearest_candidate': nearest.tolist() if nearest is not None else None}
        ordered = candidates[np.argsort(distances, kind='stable')]
        ordered = ordered[np.isfinite(ordered).all(axis=1) &
                          (np.max(np.abs(ordered - joints[-1]), axis=1) <= max_joint_step)]
        for rank, candidate in enumerate(ordered):
            joints.append(candidate)
            siblings = ordered[rank+1:rank+batch_size] if rank and solve_batch is not None else ()
            result = explore(index + 1, siblings)
            if result is not None:
                return result
            joints.pop()
            if attempts >= max_paths:
                break
        return None

    result = explore(0)
    if diagnostics is not None:
        diagnostics.update(candidate_paths_tested=attempts, ik_calls=ik_calls)
        if solve_batch is not None:
            diagnostics['ik_batches'] = ik_batches
        if ik_budget_exhausted:
            diagnostics['ik_budget_exhausted'] = True
        if blocked:
            diagnostics['farthest_blocked_waypoint'] = blocked[max(blocked)]
    if timed_out:
        raise TimeoutError(f'cuRobo linear branch search exceeded planning time after {attempts} paths and {ik_calls} IK calls')
    if result is None:
        reason = str(last_rejection) if last_rejection is not None else 'no continuous collision-free IK branch'
        if ik_budget_exhausted:
            reason += f'; IK call budget exhausted ({max_ik_calls})'
        if solve_batch is not None:
            reason += f'; {ik_batches} GPU IK batches'
        if blocked:
            reason += f'; farthest blocked waypoint: {blocked[max(blocked)]}'
        raise LinearBranchNotFound(f'cuRobo linear branch search failed after {attempts} paths and {ik_calls} IK calls: {reason}')
    return result


def timed_joint_line(waypoints, limits, *, velocity_scale, acceleration_scale,
                     resolution_rad, cartesian_step=0.002, max_sample_dt=0.02):
    """Sample a stop-at-waypoint C2 joint path within velocity, acceleration and jerk bounds."""
    joints = np.asarray(waypoints, dtype=float)
    if joints.ndim != 2 or len(joints) < 2 or not np.isfinite(joints).all():
        raise ValueError('IK returned invalid joint waypoints')
    bounds = []
    for name, scale in (('velocity', velocity_scale), ('acceleration', acceleration_scale), ('jerk', 1.)):
        lower, upper = np.asarray(limits[name], dtype=float)
        bound = np.minimum(-lower, upper) * scale
        if bound.shape != (joints.shape[1],) or not np.isfinite(bound).all() or np.any(bound <= 0):
            raise ValueError(f'Invalid {name} bounds for cuRobo linear motion')
        bounds.append(bound)
    velocity_limit, acceleration_limit, jerk_limit = bounds
    if not np.isfinite(resolution_rad) or resolution_rad <= 0 or cartesian_step <= 0 or max_sample_dt <= 0:
        raise ValueError('Linear motion sampling intervals must be positive')
    samples, timestamps, velocities, accelerations, jerks = [], [], [], [], []
    elapsed = 0.
    for index, (start, end) in enumerate(zip(joints[:-1], joints[1:])):
        delta = end - start
        magnitude = np.abs(delta)
        duration = max(0.025,
                       np.max(1.875 * magnitude / velocity_limit),
                       np.sqrt(np.max(5.774 * magnitude / acceleration_limit)),
                       np.cbrt(np.max(60. * magnitude / jerk_limit))) * 1.01
        steps = max(2, int(np.ceil(duration / max_sample_dt)),
                    int(np.ceil(1.875 * np.max(magnitude) / min(resolution_rad, 0.02))),
                    int(np.ceil(1.875 * np.linalg.norm(delta) / cartesian_step)))
        if len(samples) + steps + 1 > 10000:
            raise ValueError('cuRobo linear trajectory exceeds collision validation capacity')
        progress = np.linspace(0., 1., steps + 1)[int(index > 0):]
        smooth = 10*progress**3 - 15*progress**4 + 6*progress**5
        speed = (30*progress**2 - 60*progress**3 + 30*progress**4) / duration
        acceleration = (60*progress - 180*progress**2 + 120*progress**3) / duration**2
        jerk = (60 - 360*progress + 360*progress**2) / duration**3
        samples.extend(start + smooth[:, None] * delta)
        timestamps.extend(elapsed + progress * duration)
        velocities.extend(speed[:, None] * delta)
        accelerations.extend(acceleration[:, None] * delta)
        jerks.extend(jerk[:, None] * delta)
        elapsed += duration
    return tuple(np.asarray(item) for item in (samples, timestamps, velocities, accelerations, jerks))


def line_spec(converted, request, horizon):
    start = np.asarray(converted['current_tool_pose'], dtype=float)
    goal_matrix = goal_in_base(request['target'], converted['base_world'], start)
    goal_position = np.asarray(goal_matrix[:3, 3], dtype=float)
    goal_rotation = Rotation.from_matrix(goal_matrix[:3, :3])
    start_rotation = Rotation.from_quat(start[3:][[1, 2, 3, 0]])
    distance = float(np.linalg.norm(goal_position - start[:3]))
    step = float(request.get('strategy_params', {}).get('cartesian_step', 0.02))
    if not np.isfinite(step) or step <= 0:
        raise ValueError('cartesian_step must be positive and finite')
    if distance > (horizon - 1) * step + 1e-9:
        raise LinearCandidateRejected(f'cuRobo linear move exceeds {horizon} trajectory knots at {step:g} m spacing; split the move')
    count = min(horizon, max(2, int(np.ceil(distance / step)) + 1))
    orientation_tolerance = np.deg2rad(request['constraints']['orientation_tolerance'])
    if (request['constraints'].get('keep_end_effector_orientation') and
            (start_rotation.inv() * goal_rotation).magnitude() > orientation_tolerance):
        raise ValueError('Fixed-orientation linear move starts with a different tool orientation')
    return start[:3], start_rotation, goal_position, goal_rotation, count


def interpolate_line(spec):
    start_position, start_rotation, goal_position, goal_rotation, count = spec
    fractions = np.linspace(0., 1., count)
    positions = start_position + fractions[:, None] * (goal_position - start_position)
    rotations = Slerp([0., 1.], Rotation.concatenate([start_rotation, goal_rotation]))(fractions)
    quaternions = rotations.as_quat()[:, [3, 0, 1, 2]]
    return positions, quaternions


def line_targets(converted, request, horizon):
    return interpolate_line(line_spec(converted, request, horizon))


def validate_line(actual_positions, actual_quaternions, targets, *, position_tolerance, orientation_tolerance):
    desired_positions, desired_quaternions = targets
    actual_positions = np.asarray(actual_positions, dtype=float)
    actual_quaternions = np.asarray(actual_quaternions, dtype=float)
    if (actual_positions.ndim != 2 or actual_positions.shape[1] != 3 or
            actual_quaternions.shape != (len(actual_positions), 4) or
            len(actual_positions) < 2 or not np.isfinite(actual_positions).all() or
            not np.isfinite(actual_quaternions).all()):
        raise ValueError('cuRobo returned invalid tool poses for linear path')
    start, goal = desired_positions[0], desired_positions[-1]
    direction = goal - start
    distance_squared = float(direction @ direction)
    fractions = (np.clip((actual_positions - start) @ direction / distance_squared, 0., 1.)
                 if distance_squared > 1e-16 else np.zeros(len(actual_positions)))
    projection = start + fractions[:, None] * direction
    line_error = float(np.max(np.linalg.norm(actual_positions - projection, axis=1)))
    endpoint_error = float(np.linalg.norm(actual_positions[-1] - goal))
    if line_error > min(.005, position_tolerance) or endpoint_error > position_tolerance:
        raise ValueError(f'cuRobo path deviates from Cartesian line (max {line_error:.4f} m; endpoint {endpoint_error:.4f} m)')
    if np.any(np.diff(fractions) < -0.005):
        raise ValueError(f'cuRobo linear path reverses direction (min fraction step {np.diff(fractions).min():.4f})')
    rotations = Rotation.from_quat(desired_quaternions[:, [1, 2, 3, 0]])
    expected = Slerp(np.linspace(0., 1., len(rotations)), rotations)(fractions)
    actual = Rotation.from_quat(actual_quaternions[:, [1, 2, 3, 0]])
    rotation_error = float(np.max((expected.inv() * actual).magnitude()))
    if rotation_error > orientation_tolerance:
        raise ValueError(f'cuRobo linear path exceeds orientation tolerance ({rotation_error:.4f} rad)')
    return {'max_position_error_m': line_error, 'goal_position_error_m': endpoint_error,
            'max_orientation_error_rad': rotation_error, 'sample_count': len(actual_positions)}
