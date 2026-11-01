"""Planning-side interface for trajectory time parameterization."""
from __future__ import annotations
from typing import Any, Mapping
import numpy as np
from .errors import PlanningError


def _scaled_joint_limit(config, key, scale_key, dof, *, multiplier_key=None, clip_to_base=False):
    """Resolve a global scale plus optional per-joint scale, with URDF caps."""
    if key not in config:
        raise ValueError(f'{key} must be resolved from the robot URDF before continuous timing')
    base = np.asarray(config[key], dtype=float)
    scale = float(config.get('_constraints', {}).get(scale_key, 1.0))
    if base.shape != (dof,) or not np.all(np.isfinite(base)) or np.any(base <= 0) or not np.isfinite(scale) or scale <= 0:
        raise ValueError(f'{key} and {scale_key} must be finite positive joint limits and scale')
    values = base * scale
    if multiplier_key:
        multipliers = np.asarray(config.get(multiplier_key, np.ones(dof)), dtype=float)
        if multipliers.shape != (dof,) or not np.all(np.isfinite(multipliers)) or np.any(multipliers <= 0):
            raise ValueError(f'{multiplier_key} must contain one finite positive multiplier per joint')
        values = values * multipliers
    if clip_to_base:
        values = np.minimum(values, base)
    return values


def continuous_motion_limits(defaults, dof):
    """Resolve shared joint limits; both global scale factors participate."""
    constraints = defaults.get('constraints', {})
    config = defaults.get('planning', {}).get('time_parameterization', {})
    limits = []
    constrained = dict(config)
    constrained['_constraints'] = constraints
    limits.append(_scaled_joint_limit(constrained, 'max_velocity', 'velocity_scale', dof,
                                      clip_to_base=True))
    limits.append(_scaled_joint_limit(constrained, 'max_acceleration', 'acceleration_scale', dof))
    return tuple(limits)


def prepare_continuous_trajectory(trajectory, move_request, *, collision_checker=None, timestep=0.002, kinematics=None):
    from control.continuous_trajectory import ContinuousJointPath
    result = dict(trajectory)
    points = np.asarray(result['waypoints'], dtype=float)
    if points.ndim != 2 or len(points) < 2 or points.shape[1] == 0 or not np.all(np.isfinite(points)):
        raise ValueError('waypoints must be finite NxDOF, N >= 2')
    velocity, acceleration = continuous_motion_limits(move_request, points.shape[1])
    timing_config = move_request.get('planning', {}).get('time_parameterization', {})
    max_jerk = timing_config.get('max_jerk')
    corner_slowdown = timing_config.get('corner_slowdown', 0.25)
    points = points[np.r_[True, np.any(np.diff(points, axis=0) != 0, axis=1)]]
    if len(points) == 1:
        references = np.repeat(points, 2, axis=0)
        duration = timestep
        validation_points = references
        profile = None
    else:
        distance = np.max(np.abs(np.diff(points, axis=0)), axis=1)
        fractions = np.r_[0., np.cumsum(distance)]
        interpolator = timing_config.get('path_interpolator', 'cubic_c2')
        profile = ContinuousJointPath(points, fractions, interpolator=interpolator)
        references, duration = profile.samples(1., 0., None, velocity, acceleration, timestep,
                                               max_jerk=max_jerk, corner_slowdown=corner_slowdown)
        progress = np.linspace(0, 1, max(len(points)*4, int(np.ceil(profile.max_slope/0.01))+1))
        # One geometrically dense monotone path is enough: check_path's
        # resolution parameter samples each segment between these points, so
        # the time-sampled references do not need a second FCL traversal.
        validation_points = profile.spline(progress)
    if kinematics is not None and move_request.get('motion', {}).get('path_type') == 'linear':
        start_pose, end_pose = kinematics.forward(points[0]), kinematics.forward(points[-1])
        origin = np.asarray(start_pose['position'])
        direction = np.asarray(end_pose['position']) - origin
        length_squared = float(direction @ direction)
        for q in validation_points:
            actual = np.asarray(kinematics.forward(q)['position'])
            fraction = np.clip(float((actual-origin) @ direction)/length_squared, 0, 1) if length_squared > 1e-16 else 0
            if np.linalg.norm(actual-origin-fraction*direction) > 0.005:
                raise PlanningError('Smoothed trajectory exceeds linear path tolerance', code='TRAJECTORY_INVALID',
                                    recoverable=True, details={'stage':'execution_preparation'})
    collision_fallback = None
    if collision_checker is not None:
        checked = collision_checker.check_path(validation_points, resolution=0.01,
            safety_distance=float(move_request.get('constraints', {}).get('safety_distance', 0.0)),
            allowed_geom_ids=getattr(collision_checker, 'allowed_target_geom_ids', None))
        if checked.in_collision:
            # A cubic spline is preferred for C2 continuity, but it can bow
            # outside a tightly constrained free-space corridor.  Retry the
            # same validated waypoints with monotone PCHIP before rejecting a
            # feasible RRT path; both candidates are checked by FCL.
            if len(points) > 1 and profile.interpolator == 'cubic_c2':
                fallback = ContinuousJointPath(points, fractions, interpolator='pchip')
                fallback_refs, fallback_duration = fallback.samples(
                    1., 0., None, velocity, acceleration, timestep,
                    max_jerk=max_jerk, corner_slowdown=corner_slowdown)
                fallback_progress = np.linspace(
                    0, 1, max(len(points)*4, int(np.ceil(fallback.max_slope/0.01))+1))
                fallback_validation = fallback.spline(fallback_progress)
                fallback_check = collision_checker.check_path(
                    fallback_validation, resolution=0.01,
                    safety_distance=float(move_request.get('constraints', {}).get('safety_distance', 0.0)),
                    allowed_geom_ids=getattr(collision_checker, 'allowed_target_geom_ids', None))
                if not fallback_check.in_collision:
                    profile, references, duration, validation_points = (
                        fallback, fallback_refs, fallback_duration, fallback_validation)
                    collision_fallback = 'pchip'
                else:
                    raise PlanningError('Continuous trajectory failed collision validation', code='TRAJECTORY_INVALID',
                        recoverable=True, details={'stage': 'execution_preparation', 'contacts': list(fallback_check.contacts)})
            else:
                raise PlanningError('Continuous trajectory failed collision validation', code='TRAJECTORY_INVALID',
                    recoverable=True, details={'stage': 'execution_preparation', 'contacts': list(checked.contacts)})
    result.update(geometric_waypoints=result['waypoints'], time_parameterized_waypoints=references.tolist(),
                  timestamps=np.linspace(0, duration, len(references)).tolist(), duration=float(duration))
    result.pop('velocities', None)
    result.pop('accelerations', None)
    result['diagnostics'] = dict(result.get('diagnostics', {}))
    result['diagnostics']['time_parameterization'] = {
        'backend': 'continuous_precomputed', 'path_interpolator': profile.interpolator if profile is not None else None,
        'collision_fallback': collision_fallback, 'max_velocity': velocity.tolist(),
        'corner_slowdown': corner_slowdown,
        'max_acceleration': acceleration.tolist(), 'max_jerk': max_jerk,
        'acceleration_limited': True, 'jerk_limited': max_jerk is not None,
        'limit_source': move_request.get('planning', {}).get('time_parameterization', {}).get('limit_source'),
    }
    return result


def prepare_interpolated_trajectory(trajectory: Mapping[str, Any], move_request: Mapping[str, Any], *, collision_checker=None):
    """Keep the geometric polyline and assign timestamps, without using Ruckig.

    Timing respects joint velocity limits but does not constrain acceleration
    or jerk at corners. Intermediate samples are not commanded to stop.
    """
    result = dict(trajectory)
    points = np.asarray(result['waypoints'], dtype=float)
    if points.ndim != 2 or len(points) < 2 or points.shape[1] == 0 or not np.all(np.isfinite(points)):
        raise ValueError('waypoints must be finite with shape (N, DOF), N >= 2')
    constraints = move_request.get('constraints', {})
    config = move_request.get('planning', {}).get('time_parameterization', {})
    scale = float(constraints.get('velocity_scale', 1.0))
    limits = np.asarray(config.get('max_velocity', np.ones(points.shape[1])), dtype=float)
    if (not np.isfinite(scale) or scale <= 0 or limits.shape != (points.shape[1],)
            or not np.all(np.isfinite(limits)) or np.any(limits <= 0)):
        raise ValueError('velocity_scale and per-joint max_velocity must be finite and positive')
    limits = np.minimum(limits * scale, limits)
    # Remove exact consecutive duplicates only; preserve every geometric corner.
    points = points[np.r_[True, np.any(np.diff(points, axis=0) != 0, axis=1)]]
    if len(points) == 1:
        points = np.repeat(points, 2, axis=0)
        stamps = np.array([0.0, 0.01])
    else:
        durations = np.max(np.abs(np.diff(points, axis=0)) / limits, axis=1)
        stamps = np.r_[0.0, np.cumsum(durations)]
    if collision_checker is not None:
        checked = collision_checker.check_path(points, resolution=0.01,
            safety_distance=float(constraints.get('safety_distance', 0.0)),
            allowed_geom_ids=getattr(collision_checker, 'allowed_target_geom_ids', None))
        if checked.in_collision:
            raise PlanningError('Interpolated trajectory failed collision validation',
                code='TRAJECTORY_INVALID', recoverable=True,
                details={'stage': 'execution_preparation', 'contacts': list(checked.contacts)})
    result['geometric_waypoints'] = result['waypoints']
    result['time_parameterized_waypoints'] = points.tolist()
    result['timestamps'] = stamps.tolist()
    result['duration'] = float(stamps[-1])
    # Never retain stale derivatives from a previously timed trajectory.
    result.pop('velocities', None)
    result.pop('accelerations', None)
    result['diagnostics'] = dict(result.get('diagnostics', {}))
    result['diagnostics']['time_parameterization'] = {
        'backend': 'linear_interpolation', 'max_velocity': limits.tolist(),
        'acceleration_limited': False, 'jerk_limited': False,
    }
    return result


def parameterize_trajectory(trajectory: Mapping[str, Any], move_request: Mapping[str, Any], parameterizer=None, *, collision_checker=None):
    """Attach Ruckig timing data while retaining the geometric path."""
    from libraries.ruckig import RuckigTimeParameterizer
    result = dict(trajectory)
    points = np.asarray(result["waypoints"], dtype=float)
    constraints = move_request.get("constraints", {})
    config = move_request.get("planning", {}).get("time_parameterization", {})
    base_velocity = np.asarray(config.get("max_velocity", np.ones(points.shape[1])), dtype=float)
    base_acceleration = np.asarray(config.get("max_acceleration", np.ones(points.shape[1])), dtype=float)
    velocity_scale = float(constraints.get("velocity_scale", 1.0))
    acceleration_scale = float(constraints.get("acceleration_scale", 1.0))
    if (base_velocity.shape != (points.shape[1],) or base_acceleration.shape != (points.shape[1],)
            or not np.all(np.isfinite(base_velocity)) or not np.all(np.isfinite(base_acceleration))
            or np.any(base_velocity <= 0)
            or np.any(base_acceleration <= 0)
            or not np.isfinite(velocity_scale) or velocity_scale <= 0
            or not np.isfinite(acceleration_scale) or acceleration_scale <= 0):
        raise ValueError('Ruckig joint limits and scales must be finite and positive')
    velocity = np.minimum(base_velocity * velocity_scale, base_velocity)
    acceleration = base_acceleration * acceleration_scale
    parameterizer = parameterizer or RuckigTimeParameterizer(
        control_cycle=config.get("control_cycle", 0.01), max_velocity=velocity.tolist(),
        max_acceleration=acceleration.tolist(), max_jerk=config.get("max_jerk"))
    timed = parameterizer.parameterize(points, velocity_scale=1.0,
        acceleration_scale=1.0, max_velocity=velocity.tolist(),
        max_acceleration=acceleration.tolist(), max_jerk=config.get("max_jerk"))
    if collision_checker is not None:
        check = collision_checker.check_path(
            timed["positions"], resolution=0.01,
            safety_distance=float(constraints.get("safety_distance", 0.0)),
            allowed_geom_ids=getattr(collision_checker, "allowed_target_geom_ids", None),
        )
        if check.in_collision:
            raise PlanningError(
                "Time-parameterized trajectory failed collision validation",
                code="TRAJECTORY_INVALID", recoverable=True,
                details={"stage": "time_parameterization", "contacts": list(check.contacts)},
            )
    result["geometric_waypoints"] = result["waypoints"]
    result["time_parameterized_waypoints"] = timed["positions"].tolist()
    result["timestamps"] = timed["timestamps"]
    result["velocities"] = timed["velocities"].tolist()
    result["accelerations"] = timed["accelerations"].tolist()
    result["duration"] = timed["duration"]
    result.setdefault("diagnostics", {})["time_parameterization"] = {"backend": "ruckig", "control_cycle": timed["control_cycle"], "max_velocity": timed["max_velocity"].tolist(), "max_acceleration": timed["max_acceleration"].tolist(), "max_jerk": timed["max_jerk"].tolist()}
    return result
